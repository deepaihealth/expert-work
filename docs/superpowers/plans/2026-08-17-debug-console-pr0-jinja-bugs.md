# 调试台重设计 PR0 —— 两条 Jinja bug 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让「Jinja 动态 prompt」从控制台端到端可用:配置页保存得进 `{{ 变量 }}`,调试台显示变量输入框并随 `inputs` 发出。

**Architecture:** 两条独立修复。(A)调试台读 manifest 时多包了一层壳,改一行 + 把测试 fixture 改成真实形状;(B)拆掉 control-plane「保存时把整份 YAML 当 Jinja 渲染」的老功能(`template_vars` 请求字段 + `ManifestLoader._render` + `ManifestTemplateError` + `MANIFEST_TEMPLATE` 映射),YAML 直接解析入库,`{{ }}` 原样保存,run 期由既有 `prompt_render.render_system_prompt` 渲染。

**Tech Stack:** admin-ui(React + vitest + testing-library)/ control-plane(FastAPI + pydantic + pytest)。

**Spec:** `docs/superpowers/specs/2026-08-17-debug-console-redesign-design.md` §一「两条 bug」、§二.5「PR0」、§四。

## Global Constraints

- D1 已拍板:**拆干净**——不是「没带 `template_vars` 就跳过」,是删掉字段、删掉渲染步骤、删掉错误类与映射。带 `template_vars` 的请求 → 422(`ManifestPayload` 已是 `extra="forbid"`)。
- `build_sandboxed_environment()` **必须保留**(`services/control-plane/src/control_plane/prompt_render.py` 的 run 期渲染在用)。
- 前端 `record.spec` 是**完整 manifest**(`{apiVersion, kind, metadata, spec}`),`readPromptJinja(m)` 读的是 `m.spec.system_prompt`;测试 fixture 必须按这个形状造。
- 每条新断言按 break → red → restore → green 自证(先在未修代码上跑红)。
- 仓库无 CHANGELOG 文件,变更记 PR 说明 + `docs/design/jinja-dynamic-prompt.md` 勘误段。
- 命令:python 一律 `uv run …`(仓库根);admin-ui 在 `apps/admin-ui` 下 `pnpm test -- <file>` / `pnpm typecheck`(裸 `tsc --noEmit` 恒绿,不算数)。
- 变异 / 自证时不要用 `git checkout --` 还原,先把文件复制到 scratchpad。

---

## 文件结构

| 文件 | 改动 |
|---|---|
| `apps/admin-ui/src/pages/agent_detail/PlaygroundTab.tsx:133-140` | 去掉多包的一层壳 |
| `apps/admin-ui/src/pages/__tests__/PlaygroundTab.test.tsx:609-620` | fixture 改真实形状 |
| `services/control-plane/src/control_plane/api/agents.py:113-117, :637-644, :685-690` | 删 `template_vars`、删 `_load_manifest` 的透传、删 `MANIFEST_TEMPLATE` 映射 |
| `services/control-plane/src/control_plane/manifest/loader.py` | 删 `_render`、`template_vars` 参数、`ManifestTemplateError` 引用、模块与函数 docstring 改写 |
| `services/control-plane/src/control_plane/manifest/errors.py` | 删 `ManifestTemplateError`,改 `ManifestSyntaxError` docstring |
| `services/control-plane/src/control_plane/manifest/__init__.py` | 删导出 |
| `services/control-plane/tests/test_manifest_loader.py` | 删两条模板用例,加两条「`{{ }}` 原样入库」 |
| `services/control-plane/tests/test_agents_api.py` | 加「保存 `{{ }}` 回读原样」+「`template_vars` → 422」 |
| `apps/admin-ui/src/api/agents.ts:97` | 删字段 |
| `docs/design/jinja-dynamic-prompt.md` §2 | 勘误段 |
| `tools/bench/README.md:137` | 去掉 `template_vars` 提法 |

---

### Task 1: 调试台变量输入框(Bug A)

**Files:**
- Modify: `apps/admin-ui/src/pages/agent_detail/PlaygroundTab.tsx:133-140`
- Test: `apps/admin-ui/src/pages/__tests__/PlaygroundTab.test.tsx:609-650`

**Interfaces:**
- Consumes: `readPromptJinja(m: unknown): boolean` / `readPromptVariables(m: unknown): PromptVariableFields[]`(`components/manifest-editor/form_model.ts:574-578`,读 `m.spec.system_prompt`)。
- Produces: 无(页面内部)。

- [ ] **Step 1: 把既有用例的 fixture 改成真实形状(这一步让它在未修代码上变红)**

`PlaygroundTab.test.tsx:609-620`,`jinjaDetail` 改为:

```ts
    const jinjaDetail: AgentDetailResponse = {
      record: {
        ...sampleDetail.record,
        // 真实 API 形状:record.spec 是完整 manifest,不是内层 spec
        // (后端 record.spec.metadata.labels 直接取用;#824 的 fixture 造错了形状,
        // 把 PlaygroundTab 多包一层壳的 bug 盖住了整整一版)。
        spec: {
          apiVersion: "expert_work.io/v1",
          kind: "Agent",
          metadata: { name: "demo-agent", version: "1.0.0", tenant: "acme" },
          spec: {
            system_prompt: {
              template: "你是 {{ persona }}",
              jinja: true,
              variables: [{ name: "persona", trusted: true, required: true }],
            },
          },
        },
      },
    };
```

- [ ] **Step 2: 跑,确认红**

Run: `cd apps/admin-ui && pnpm test -- src/pages/__tests__/PlaygroundTab.test.tsx -t "renders declared prompt variables"`
Expected: FAIL —— `Unable to find an element by: [data-testid="playground-var-persona"]`。

- [ ] **Step 3: 修 `PlaygroundTab.tsx:133-140`**

把

```ts
  // Dynamic-Prompt — the agent's declared run-time variables (jinja agents only).
  const manifestLike = { spec: r.spec };
  const promptJinja = readPromptJinja(manifestLike);
  const promptVariables = promptJinja
    ? readPromptVariables(manifestLike).filter(
        (v): v is { name: string } & typeof v => Boolean(v.name),
      )
    : [];
```

改为

```ts
  // Dynamic-Prompt — the agent's declared run-time variables (jinja agents only).
  // ``record.spec`` IS the full manifest ({apiVersion, kind, metadata, spec}), so it
  // is passed to the form_model readers as-is — wrapping it in another ``{ spec }``
  // shell made ``readPromptJinja`` look at ``manifest.system_prompt`` (undefined) and
  // hid the variable inputs for every jinja agent (#824 → PR0 of the console redesign).
  const promptJinja = readPromptJinja(r.spec);
  const promptVariables = promptJinja
    ? readPromptVariables(r.spec).filter(
        (v): v is { name: string } & typeof v => Boolean(v.name),
      )
    : [];
```

- [ ] **Step 4: 跑,确认绿;再跑全文件**

Run: `cd apps/admin-ui && pnpm test -- src/pages/__tests__/PlaygroundTab.test.tsx`
Expected: 全绿(该文件所有用例)。

- [ ] **Step 5: 加一条守形状的用例:内层 spec 形状(旧 fixture 那种)不再被当成 jinja agent**

在同一 `describe` 里、Step 1 那条用例之后加:

```ts
  it("does not treat a bare inner spec as a jinja agent (record.spec is the full manifest)", async () => {
    // 如果有人把 record.spec 造成内层 spec(旧 fixture 的形状),变量框不能出现——
    // 这条守住「readers 读的是 manifest.spec.system_prompt」这个约定。
    const innerShape: AgentDetailResponse = {
      record: {
        ...sampleDetail.record,
        spec: {
          system_prompt: {
            template: "你是 {{ persona }}",
            jinja: true,
            variables: [{ name: "persona" }],
          },
        },
      },
    };
    renderPg(innerShape);
    await screen.findByTestId("playground-input");
    expect(screen.queryByTestId("playground-vars")).not.toBeInTheDocument();
  });
```

- [ ] **Step 6: 跑,确认绿;typecheck**

Run: `cd apps/admin-ui && pnpm test -- src/pages/__tests__/PlaygroundTab.test.tsx && pnpm typecheck`
Expected: PASS / 无类型错误。

- [ ] **Step 7: Commit**

```bash
git add apps/admin-ui/src/pages/agent_detail/PlaygroundTab.tsx apps/admin-ui/src/pages/__tests__/PlaygroundTab.test.tsx
git commit -m "fix(playground): Jinja 变量输入框永不渲染 —— record.spec 已是完整 manifest,去掉多包的一层壳

#824 引入即坏:readPromptJinja({spec: record.spec}) 去读 manifest.system_prompt(undefined)。
测试 fixture 当时按内层 spec 造,把 bug 盖住;本次 fixture 改成真实形状并加一条守形状用例。"
```

---

### Task 2: 拆掉「保存时填空」(Bug B,后端)

**Files:**
- Modify: `services/control-plane/src/control_plane/manifest/loader.py`
- Modify: `services/control-plane/src/control_plane/manifest/errors.py`
- Modify: `services/control-plane/src/control_plane/manifest/__init__.py`
- Modify: `services/control-plane/src/control_plane/api/agents.py:113-117, :637-644, :685-690`(以及 `:68` 的 import)
- Test: `services/control-plane/tests/test_manifest_loader.py`、`services/control-plane/tests/test_agents_api.py`

**Interfaces:**
- Produces: `ManifestLoader.load_from_string(source: str) -> AgentSpec`、`ManifestLoader.load_from_path(path: str | Path) -> AgentSpec`、`load_manifest(source: str | Path, *, max_size_bytes: int = DEFAULT_MAX_SIZE_BYTES) -> AgentSpec`(**无** `template_vars`);`ManifestPayload { manifest_yaml: str }`;`build_sandboxed_environment()` 不变。
- Consumes: 无。

- [ ] **Step 1: 先写红测试(loader 层)**

`services/control-plane/tests/test_manifest_loader.py`:

(a)把顶部的 `_MINIMAL_TEMPLATE` 与 `_rendered_minimal()` 换成一个不含占位符的常量:

```python
_MINIMAL_YAML = """\
apiVersion: expert_work.io/v1
kind: Agent
metadata:
  name: code-reviewer
  version: "1.0.0"
  tenant: platform-eng
spec:
  tenant_config: {}
  model:
    provider: anthropic
    name: claude-sonnet-4-5
  system_prompt:
    template: "you are a reviewer"
  sandbox:
    resources: { cpu: "1.0", memory: "1Gi" }
    network:
      egress: proxy
      allowlist: ["api.anthropic.com"]
    filesystem:
      readonly_root: true
      writable: ["/workspace"]
"""
```

文件里所有 `_rendered_minimal()` 全部改成 `_MINIMAL_YAML`(共三处:`test_load_minimal_yaml`、`test_load_from_path`、`test_pydantic_validation_error_surfaces`)。

(b)删除 `test_template_variable_substituted` 与 `test_undefined_template_var_raises` 两条,删除 import 里的 `ManifestTemplateError`。

(c)在「happy paths」区加两条:

```python
def test_double_braces_in_system_prompt_survive_verbatim() -> None:
    """Jinja 动态 prompt 的 {{ }} 是 run 期语义:保存时必须原样入库,不能被当成
    manifest 变量求值(调试台重设计 PR0 Bug B —— 「保存时填空」整层已拆掉)。"""
    yaml_text = _MINIMAL_YAML.replace(
        'template: "you are a reviewer"',
        'template: "you are {{ persona }}"\n    jinja: true\n    variables: [{name: persona}]',
    )
    spec = load_manifest(yaml_text)
    assert spec.spec.system_prompt.template == "you are {{ persona }}"
    assert spec.spec.system_prompt.jinja is True
    assert [v.name for v in spec.spec.system_prompt.variables] == ["persona"]


def test_double_braces_survive_even_when_jinja_is_off() -> None:
    """jinja 关着时 {{ }} 也只是普通文本,同样原样入库(以前会 ManifestTemplateError)。"""
    yaml_text = _MINIMAL_YAML.replace('"you are a reviewer"', '"literal {{ not_a_var }}"')
    spec = load_manifest(yaml_text)
    assert spec.spec.system_prompt.template == "literal {{ not_a_var }}"
```

- [ ] **Step 2: 跑,确认红**

Run: `uv run pytest services/control-plane/tests/test_manifest_loader.py -q`
Expected: 两条新用例 FAIL(旧代码抛 `ManifestTemplateError: manifest template render failed: 'persona' is undefined`);另外 import 处因 `ManifestTemplateError` 已从 import 删除而 —— 注意:旧代码里它仍存在,所以 import 删除不会报错;只看两条新用例红即可。

- [ ] **Step 3: 改 `loader.py`**

(a)模块 docstring 第 1–17 行改为:

```python
"""YAML → :class:`AgentSpec`.

Stages:

1. **Size guard** — refuse documents larger than ``max_size_bytes`` (DoS
   protection per STREAM-B-DESIGN § 6).
2. **YAML parse** — ``yaml.safe_load``, never ``yaml.load``.
3. **Pydantic validation** — :class:`AgentSpec` carries the lint rules
   (network allowlist + fallback-chain cycles) as ``model_validator``\\s.

There is deliberately **no** template-rendering stage: ``{{ … }}`` in a
manifest is run-time Jinja (``system_prompt.jinja`` + request ``inputs``,
rendered by :mod:`control_plane.prompt_render`), never a save-time
substitution. The former save-time ``template_vars`` pass was removed in
the 2026-08-17 console-redesign PR0 (zero callers; it swallowed every
``{{ }}`` a jinja agent's prompt legitimately carries).
"""
```

(b)import 区:删 `from collections.abc import Mapping`、删 `TemplateError`(`from jinja2 import StrictUndefined, select_autoescape`),删 `ManifestTemplateError`(`from control_plane.manifest.errors import ManifestSyntaxError, ManifestValidationError`)。`Any` 仍被 `_parse_yaml` 返回类型用到,保留。

(c)`build_sandboxed_environment` docstring 第一句改为:

```python
    """The one SSTI-safe Jinja2 environment used for the run-time
    ``system_prompt`` render (:mod:`control_plane.prompt_render`).
```

其余不动。

(d)`load_from_string` / `load_from_path` / `_render` / `load_manifest` 改为:

```python
    def load_from_string(self, source: str) -> AgentSpec:
        encoded = source.encode("utf-8")
        if len(encoded) > self._max_size_bytes:
            msg = f"manifest exceeds size cap {len(encoded)} > {self._max_size_bytes} bytes"
            raise ManifestSyntaxError(msg)

        document = self._parse_yaml(source)
        return self._validate(document)

    def load_from_path(self, path: str | Path) -> AgentSpec:
        return self.load_from_string(Path(path).read_text(encoding="utf-8"))
```

删除整个 `_render` 方法(`:104-116`)。

```python
def load_manifest(
    source: str | Path,
    *,
    max_size_bytes: int = DEFAULT_MAX_SIZE_BYTES,
) -> AgentSpec:
    """Convenience wrapper for one-off loads (tests, CLI lint)."""
    loader = ManifestLoader(max_size_bytes=max_size_bytes)
    if isinstance(source, Path):
        return loader.load_from_path(source)
    return loader.load_from_string(source)
```

- [ ] **Step 4: 改 `errors.py` 与 `__init__.py`**

`errors.py`:删掉 `ManifestTemplateError` 类(含 docstring);`ManifestSyntaxError` docstring 改为 `"""YAML failed to parse, or the parsed root is not a mapping."""`。

`__init__.py`:删掉 import 列表与 `__all__` 里的 `"ManifestTemplateError"`。

- [ ] **Step 5: 跑 loader 测试与 ruff**

Run: `uv run pytest services/control-plane/tests/test_manifest_loader.py -q && uv run ruff check services/control-plane/src/control_plane/manifest && uv run ruff format --check services/control-plane/src/control_plane/manifest`
Expected: loader 测试全绿;ruff 无未使用 import 报警。(此时 `api/agents.py` 还没改,它 import 的 `ManifestTemplateError` 已不存在 —— control-plane 整体 import 会失败,这正是下一步 API 测试红的一部分,先不管。)

- [ ] **Step 6: 写 API 层红测试**

`services/control-plane/tests/test_agents_api.py`,在 `_VALID_YAML` 之后加常量,在「create」区加两条:

```python
_JINJA_YAML = _VALID_YAML.replace(
    'template: "you are a reviewer"',
    'template: "you are {{ persona }}"\n    jinja: true\n    variables:\n      - name: persona\n        required: true',
)
```

```python
@pytest.mark.asyncio
async def test_post_keeps_jinja_braces_verbatim(b5_client: AsyncClient) -> None:
    """Jinja 动态 prompt 的 {{ }} 属于 run 期(prompt_render),保存时必须原样入库。
    控制台保存带 {{ }} 的 prompt 曾一律 400 MANIFEST_TEMPLATE(调试台重设计 PR0 Bug B)。"""
    response = await b5_client.post("/v1/agents", json={"manifest_yaml": _JINJA_YAML})
    assert response.status_code == 201, response.text
    detail = await b5_client.get("/v1/agents/code-reviewer/1.0.0")
    assert detail.status_code == 200
    prompt = detail.json()["data"]["record"]["spec"]["spec"]["system_prompt"]
    assert prompt["template"] == "you are {{ persona }}"
    assert prompt["jinja"] is True
    assert [v["name"] for v in prompt["variables"]] == ["persona"]


@pytest.mark.asyncio
async def test_post_rejects_removed_template_vars_field(b5_client: AsyncClient) -> None:
    """``template_vars`` 已下线;ManifestPayload 是 extra=forbid,带它的请求 422。"""
    response = await b5_client.post(
        "/v1/agents",
        json={"manifest_yaml": _VALID_YAML, "template_vars": {"name": "x"}},
    )
    assert response.status_code == 422
```

- [ ] **Step 7: 跑,确认红**

Run: `uv run pytest services/control-plane/tests/test_agents_api.py -k "jinja_braces or template_vars" -q`
Expected: 收集阶段就 ERROR(`ImportError: cannot import name 'ManifestTemplateError'`,因为 `api/agents.py:68` 还 import 它)。这就是红。若想看到「逻辑上的红」,可以临时把 `agents.py:68` 那一行的 `ManifestTemplateError` 删掉再跑一次:第一条 FAIL(`TypeError: load_from_string() got an unexpected keyword argument 'template_vars'` → 500)、第二条 FAIL(`201 != 422`)。两种红都成立,不必纠结。

- [ ] **Step 8: 改 `api/agents.py`**

`:113-117`:

```python
class ManifestPayload(BaseModel):
    """``POST/PUT /v1/agents`` body — the manifest YAML text, nothing else.

    ``{{ … }}`` inside the YAML is run-time Jinja for jinja agents (rendered
    per run from ``inputs``); the loader stores it verbatim. The former
    save-time ``template_vars`` field was removed (console-redesign PR0).
    """

    model_config = ConfigDict(extra="forbid")

    manifest_yaml: str = Field(min_length=1)
```

`:637-644` `_load_manifest`:

```python
async def _load_manifest(
    payload: ManifestPayload,
    loader: ManifestLoader,
) -> tuple[Any, str]:
    """Parse the request body into an ``AgentSpec`` + canonical sha256."""
    spec = loader.load_from_string(payload.manifest_yaml)
    spec_json = spec.model_dump(by_alias=True, mode="json")
    return spec, _spec_sha256(spec_json)
```

`:685-690`:删掉 `if isinstance(exc, ManifestTemplateError): return _envelope_error("MANIFEST_TEMPLATE", …, 400)` 整个分支;`:68` import 里删 `ManifestTemplateError`。

- [ ] **Step 9: 跑,确认绿;再全量跑 control-plane**

Run: `uv run pytest services/control-plane/tests/test_agents_api.py -k "jinja_braces or template_vars" -q`
Expected: 2 passed。

Run: `uv run pytest services/control-plane/tests -q -n auto --timeout=120 && uv run ruff check services/control-plane && uv run ruff format --check services/control-plane`
Expected: 全绿(`test_external_run_inputs.py::test_inputs_reaches_prompt_render` 与 `test_prompt_render.py` 一起构成「保存原样 → run 期渲染」的端到端证据链);ruff 无报警。

- [ ] **Step 10: Commit**

```bash
git add services/control-plane/src/control_plane/manifest services/control-plane/src/control_plane/api/agents.py services/control-plane/tests/test_manifest_loader.py services/control-plane/tests/test_agents_api.py
git commit -m "fix(manifest): 拆掉「保存时填空」整层 —— {{ }} 原样入库,交给 run 期 Jinja

ManifestLoader 以前把整份 YAML 先当 Jinja(StrictUndefined)渲染再解析,是给 API 调用方用的
template_vars 老功能(全仓零使用者)。它把 jinja 动态 prompt 里合法的 {{ var }} 当未定义变量,
控制台保存带 {{ }} 的 prompt 一律 400 MANIFEST_TEMPLATE;用户被迫写单花括号,而单花括号在
run 期根本不会被替换。删:template_vars 字段 / _render / ManifestTemplateError / MANIFEST_TEMPLATE。
保留 build_sandboxed_environment(prompt_render 在用)。"
```

---

### Task 3: 清尾 —— 前端类型、设计文档勘误、bench README

**Files:**
- Modify: `apps/admin-ui/src/api/agents.ts:97`
- Modify: `docs/design/jinja-dynamic-prompt.md`(§2 末尾)
- Modify: `tools/bench/README.md:137`

**Interfaces:** 无。

- [ ] **Step 1: 删前端类型字段**

`apps/admin-ui/src/api/agents.ts:96-97` 附近,把 `template_vars?: Record<string, unknown> | null;` 这一行删掉(保留 `manifest_yaml: string;`)。全仓 grep 确认没有调用点:

Run: `rg -n "template_vars" apps/admin-ui/src`
Expected: 无输出。

- [ ] **Step 2: typecheck**

Run: `cd apps/admin-ui && pnpm typecheck`
Expected: 无错误。

- [ ] **Step 3: 设计文档勘误**

`docs/design/jinja-dynamic-prompt.md` §2「现状(已查实)」列表末尾追加一条:

```markdown
- **勘误(2026-08-17,调试台重设计 PR0)**:上面那条「已有沙箱 Jinja 渲染器只在建/改 agent 时渲染」
  的老功能与本设计**撞车**——它把整份 YAML(含 `system_prompt.template`)当 Jinja 用 `StrictUndefined`
  渲染,jinja 动态 prompt 里合法的 `{{ var }}` 在保存时被当未定义变量,控制台一律 400
  `MANIFEST_TEMPLATE`;M2 前端又把 `record.spec` 多包了一层壳,调试台的变量框从未渲染。两条合起来,
  本功能在 PR #824 之后从未端到端可用。PR0 的处理:**保存时填空整层下线**(`template_vars` 字段 /
  `ManifestLoader._render` / `ManifestTemplateError` 全删,YAML 直接解析入库),`{{ }}` 从此只有一个
  含义 = run 期变量;调试台读 manifest 的壳去掉。单花括号 `{var}` 从来不是变量,已这样写的 agent 要改回
  `{{ var }}`。
```

- [ ] **Step 4: bench README**

`tools/bench/README.md:137` 把

```
   只收 `{"manifest_yaml": "...", "template_vars": {...}}`,发 `{"manifest":
```

改为

```
   只收 `{"manifest_yaml": "..."}`,发 `{"manifest":
```

- [ ] **Step 5: 全仓兜底 grep**

Run: `rg -n "template_vars|ManifestTemplateError|MANIFEST_TEMPLATE" --glob '!docs/superpowers/**' --glob '!node_modules' .`
Expected: 无输出(`docs/superpowers/` 下的 spec / plan / ROADMAP 是历史记录,允许保留)。

- [ ] **Step 6: Commit**

```bash
git add apps/admin-ui/src/api/agents.ts docs/design/jinja-dynamic-prompt.md tools/bench/README.md
git commit -m "chore(jinja): 清掉 template_vars 残留 —— 前端类型字段、设计文档勘误、bench README"
```

---

## 完成判据(PR 说明里要写的)

- 调试台:配了 `jinja: true` + 变量的 agent 打开调试台能看到变量框;发送请求体带 `inputs`(既有用例 + 真实形状 fixture)。
- 控制台配置页:系统提示词里写 `{{ customer_code }}` 能保存;回读原样。
- 接口变更:`POST/PUT /v1/agents` 不再接受 `template_vars`(422);`MANIFEST_TEMPLATE` 错误码不再出现。
- 用户侧提示:此前用单花括号 `{customer_code}` 绕过去的 agent,要改回 `{{ customer_code }}`(单花括号从来不会被替换)。
