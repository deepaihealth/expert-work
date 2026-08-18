# 动态 Prompt — run 期 Jinja 变量注入

> 状态:M1 后端 + M2 前端已实现(PR #824);2026-08-18 勘误:此前从未端到端可用,见 §2 末条(调试台重设计 PR0 修复)。
> 决策人:owner(leyi)。安全姿态为 owner 明确选择,见 §4。

## 1. 目标

让 agent 的 system prompt 支持 Jinja 模板:`{{ var }}` 占位符在**调用 run 接口时**由请求体 `inputs` 动态填充,实现「同一个 agent + 不同上下文 = 不同 system prompt」。对标 Dify 的「Jinja 开关 + 输入变量」面板。

典型用法(业务系统按会话注入角色名 / 客户画像 / 销售阶段):

```jinja
**角色名:** {{ self_appellation }}
## 客户画像
{{ profile_summary }}
当前销售阶段:{{ stage }}
```

调用:
```http
POST /v1/sessions/{thread_id}/runs
{ "input": "帮我看看这个客户", "inputs": { "self_appellation": "深护智康顾问", "profile_summary": "30岁,关注三高", "stage": "需求挖掘" } }
```

## 2. 现状(已查实)

- `SystemPromptSpec.template`(`agent_spec.py:176`)schema 层是**纯字符串**,无占位符语义。
- 已有沙箱 Jinja 渲染器 `control_plane/manifest/loader.py:94`(`SandboxedEnvironment` + `StrictUndefined` + autoescape-off),挡 SSTI/RCE —— **但只在「建/改 agent」时渲染**(`template_vars` 烧死进存库 AgentSpec),run 期无变量。(**已下线**,见本节末勘误)
- `RunRequest`(`runs.py:87`,`extra="forbid"`)字段:input / mode / image_refs / untrusted_content。**无 inputs dict**。run 动态数据全进 HumanMessage,从不碰 SystemMessage。
- jinja2 已是 control-plane 依赖(`pyproject.toml:28`),orchestrator 未引。
- `protocol` 有 `DynamicContextSpec` / `CustomReminderSpec.template`(`agent_spec.py:182`)占位字段,但 **orchestrator 零消费者**(不复用,避免混淆)。
- **勘误(2026-08-18,调试台重设计 PR0)**:上面那条「已有沙箱 Jinja 渲染器只在建/改 agent 时渲染」
  的老功能与本设计**撞车**——它把整份 YAML(含 `system_prompt.template`)当 Jinja 用 `StrictUndefined`
  渲染,jinja 动态 prompt 里合法的 `{{ var }}` 在保存时被当未定义变量,控制台一律 400
  `MANIFEST_TEMPLATE`;M2 前端又把 `record.spec` 多包了一层壳,调试台的变量框从未渲染。两条合起来,
  本功能在 PR #824 之后从未端到端可用。PR0 的处理:**保存时填空整层下线**(`template_vars` 字段 /
  `ManifestLoader._render` / `ManifestTemplateError` 全删,YAML 直接解析入库),`{{ }}` 从此只有一个
  含义 = run 期变量;调试台读 manifest 的壳去掉。单花括号 `{var}` 从来不是变量,已这样写的 agent 要改回
  `{{ var }}`。

**核心 gap**:现有 Jinja 是「建 agent 期」注入点;本设计新增「run 期」注入点。两者独立。

## 3. 架构:只渲染「人写的 base」,不碰「平台自动拼接的 suffix」

发给 LLM 的最终 system prompt 是拼出来的(`agent_factory.py:670` `_assemble_system_prompt`):

```
final_system_prompt = base                       ← 人写的 system_prompt.template(平台管理员 or 租户,谁建谁写)
                    + suffix(spotlight 子句 + 技能摘要/正文 + behavior patch + 记忆块)
                                                  ← 没人写,orchestrator 构建时按「绑了啥技能/有没有记忆」算出来的
```

`_assemble_system_prompt` 的 `pieces[0] == base`,其余全是追加 → `final_system_prompt == base + suffix`,故 `suffix = final_system_prompt[len(base):]`,**零改 assembly 内部**即可切分。

**Jinja 渲染只作用于 base,suffix 渲染后原样追加、自身从不过 Jinja。** 理由:

1. **隔离炸裂**:suffix 里的技能正文/记忆块可能含字面 `{{ }}`(如一个教 Jinja 的技能)。整段一起渲染 → 那些 `{{ x }}` 触发 `StrictUndefined` 抛错,无关技能能搞炸 run。
2. **缩小 SSTI 攻击面**:记忆 / 技能正文是**可能含不可信内容**的通道。只渲染 base = 它们永不被 Jinja 求值,塞 `{{ ''.__class__.__mro__ }}` 也只是文字。攻击面仅剩作者自己写的 base。
3. **贴合 UI 语义**:Jinja 开关在 SYSTEM 编辑框上 = 只管 base,平台拼接非用户可编辑内容。

## 4. 安全姿态(owner 拍板,future 不许擅改默认)

变量声明带 `trusted` 标志,**默认 `trusted: true`**:

- `trusted: true`(默认)→ 值**直渲**进 base,不加围栏。
- `trusted: false`(opt-in 硬化)→ 值先经 `spotlight_untrusted(value, nonce)`(datamark + 每 run nonce 围栏)再渲染,LLM 当数据不当指令。spotlight 关时(nonce None)降级为 `[untrusted content]` 明文标记(同 `untrusted_content` 现有降级)。

owner 原话:「不想做太多限制,遇到风险人为控制,更灵活」。正则做注入检测已劝退(语义绕过 / 误判正常业务文本 / CodeQL log 雷)。这契合平台原则 `feedback_audit_over_blocking`:allow + 审计,硬化做可选。

**默认不 fence ⇒ 渲染快照审计成了唯一安全网**,故审计必须做实(§6)。

**已知风险(owner 接受)**:默认 trusted=true 让外部值进 SystemMessage = 绕过 PI-1 spotlight;配错静默高危;prompt cache 击穿(base 是前缀,值变即换前缀,仅 jinja agent 受影响,非 jinja agent byte-identical)。

## 5. Schema 与数据流

### 5.1 protocol(`agent_spec.py`)

```python
class PromptVariableSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, pattern=r"^[a-zA-Z_][a-zA-Z0-9_]*$")  # 合法 Jinja 标识符
    trusted: bool = True          # owner 拍板默认 true
    required: bool = True         # false → 缺失时渲染为空串,不报错
    description: str | None = None

class SystemPromptSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    template: str = Field(min_length=1)
    jinja: bool = False                                    # 总开关(对应 UI Jinja toggle)
    variables: list[PromptVariableSpec] = Field(default_factory=list)

    @model_validator(mode="after")
    def _variables_require_jinja(self):
        if self.variables and not self.jinja:
            raise ValueError("variables declared but jinja mode is off")
        names = [v.name for v in self.variables]
        if len(names) != len(set(names)):
            raise ValueError("duplicate prompt variable names")
        return self
```

`jinja=False`(所有现存 agent)→ 渲染整条链路全跳过,**byte-identical** 现状,cache 不动。Tier 归属:`system_prompt` 是 tenant_owned(`agent_template_resolve.py:80`),子字段继承。

### 5.2 run 请求(`runs.py` `RunRequest`)

```python
inputs: dict[str, Any] = Field(default_factory=dict)

@field_validator("inputs")
@classmethod
def _bound_inputs(cls, v):
    if len(v) > 64: raise ValueError("too many input variables (max 64)")
    for key, val in v.items():
        if isinstance(val, str) and len(val) > 8192:
            raise ValueError(f"input '{key}' exceeds 8192 chars")
    return v
```

### 5.3 BuiltAgent(`agent_factory.py:139`)新增

```python
prompt_jinja: bool = False
prompt_variables: tuple[PromptVariableSpec, ...] = ()
prompt_base: str = ""        # 未渲染的 base(jinja 时含 {{ }})
prompt_suffix: str = ""      # final_system_prompt[len(base):]
```

build 时(~746)填充:`prompt_jinja=spec.spec.system_prompt.jinja`,`prompt_variables=tuple(spec.spec.system_prompt.variables)`,`prompt_base=spec.spec.system_prompt.template`,`prompt_suffix=final_system_prompt[len(base):]`。`system_prompt`(全量)保留供非 jinja 路径。

### 5.4 渲染 helper(control-plane 新模块,复用 loader 的 SandboxedEnvironment)

抽 `control_plane/prompt_render.py`:

```python
def render_system_prompt(built, inputs: dict) -> str:
    if not built.prompt_jinja:
        return built.system_prompt                       # 非 jinja:原样,零开销
    ctx = {}
    declared = {v.name: v for v in built.prompt_variables}
    for name, spec in declared.items():
        raw = inputs.get(name, "")
        if spec.trusted:
            ctx[name] = raw
        else:
            ctx[name] = _fence_value(raw, nonce=built.spotlight_nonce)
    rendered_base = _sandboxed_render(built.prompt_base, ctx)   # 复用 loader 的 env 构造
    return rendered_base + built.prompt_suffix
```

`_sandboxed_render` 与 `loader._render` 同款 `SandboxedEnvironment`(挡 SSTI)。抽公共构造,**不写第二份裸 Environment**。

### 5.5 校验(请求期 422,queue 也提前拒)

`validate_prompt_inputs(built, inputs)` 在 `spawn_run` 早段(image 校验后)调用,raise `HTTPException(422)`:

- `inputs` 非空但 `prompt_jinja` False → "agent declares no prompt variables"。
- `inputs` 含未声明 key → "unknown input variable: X"。
- 声明 `required=True` 但 `inputs` 缺 → "missing required input: X"。

queue 模式在入队**前**校验 → 客户端同步拿 422,不到 worker 才炸。

### 5.6 渲染落点(stream + queue 单一源)

`build_run_graph_input`(`runs.py:263`)加 `inputs` 参,`SystemMessage` 内容改 `render_system_prompt(built, inputs)`:

```python
SystemMessage(content=render_system_prompt(built, inputs)),
```

两调用点传 inputs:
- stream:`spawn_run`(`runs.py:688`)→ `build_run_graph_input(..., inputs=payload.inputs)`。
- queue:`enqueued_input`(`runs.py:665`)加 `"inputs": payload.inputs`;worker(`run_queue_worker.py:209`)读 `payload.get("inputs") or {}` 传入。**漏了则 stream/queue 行为分叉。**

## 6. 审计(默认不 fence 的唯一安全网)

`spawn_run` 的 `run.start` 审计(`runs.py:643`)details 增:

```python
"prompt_jinja": built.prompt_jinja,
"prompt_var_names": [v.name for v in built.prompt_variables],
"rendered_prompt_sha256": sha256(rendered),    # 篡改取证,不存明文内容
```

存**变量名 + 渲染后 SHA256**(篡改证据 + 哪些变量参与),不存值/明文 —— 避免在审计建新 PII/secret sink,也躲 CodeQL clear-text-logging。完整复现 = 用同 inputs 重渲(模板在 spec、inputs 在调用方业务系统)。审计 details 是 DB 数据存储非 python logging,不踩 log-injection。

## 7. 前端(admin-ui)

- manifest 表单 system_prompt 区加「Jinja」开关 + 「输入变量」清单(参 Dify 截图):每行 name + trusted Switch + required Switch + description。复用现有 Form.List 范式(参 `CapabilityPickers.tsx` 子 agent 行)。
- Playground 可加「变量」输入区(jinja agent 时显示声明的变量,填值随 run 带 `inputs`)。
- i18n 三同步;表单元素 aria-label(axe critical);testid。

## 8. 范围与里程碑

- **M1 后端**:protocol schema + BuiltAgent 字段 + prompt_render helper + RunRequest.inputs + 校验 + 渲染落点 + queue 持久化 + 审计。无 migration(schema 进 spec JSONB)。
- **M2 前端**:Jinja 开关 + 变量清单表单 + Playground 变量输入。

## 9. 验证

- pytest 单测:schema 校验(variables 无 jinja 报错 / 重名);`render_system_prompt`(非 jinja 原样 / trusted 直渲 / untrusted 围栏 / suffix 不渲染 / SSTI 串当文字);`validate_prompt_inputs`(未声明 / 缺 required → 422);queue round-trip inputs。
- 手动真栈:建 jinja agent,run 带 inputs,抓真实发给 LLM 的 system prompt 确认变量替换 + suffix 完整 + trusted=false 值带围栏。

## 10. 不做(本轮范围外)

- 复用 `DynamicContextSpec.custom_reminders`(占位字段,留 backlog,别混淆)。
- 强制动态块放尾部 / 保 prompt cache(作者纪律,不做技术约束 —— owner 拍板)。
- 正则注入检测(已劝退)。
- HumanMessage / tool 参数的 Jinja(本轮只 system prompt)。
