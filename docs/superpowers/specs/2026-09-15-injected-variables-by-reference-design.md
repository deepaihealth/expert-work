# B-61 注入变量按引用给沙箱 + 声明变量绑定到工具参数 —— 设计

**状态**:2026-09-15 brainstorm 定稿,待实施计划。
**前置**:B-60 写入侧隔离已上线测试环境(`621249f6`)—— 本设计依赖「`/workspace` 就是本 agent 自己的目录」。

---

## 零、读之前要知道的(都是本轮在代码/真栈里核过的,不是推测)

1. **MCP 工具参数只能由模型生成**。`tools/mcp.py:539` 是 `session.call_tool(name, dict(args))`,args 原样来自 LLM 的 tool_call,中间**没有任何注入层**;MCP 只有 `headers`/`auth_config` 是 operator 注入的。所以「把值搬进沙箱文件」只解决沙箱侧,宿主侧工具一个字也拿不到。
2. **声明变量今天只管提示词渲染**。`PromptVariableSpec`(`packages/expert-work-protocol/.../agent_spec.py:188`)只有 `name`/`trusted`/`required`/`description`;渲染在 `control_plane/prompt_render.py:42`,校验在同文件 `:79`。跟工具参数没有任何关系。
3. **值已经到执行层,不需要新管道**。`prompt_inputs` 从两条路都汇进 orchestrator 的 run 入口(`api/runs.py:1290` 直跑、`run_queue_worker.py:397` 队列)→ `orchestrator/sse.py:334`;而 sse.py `:376-383` 正是组装 `config["configurable"]` 的地方。
4. **`MCPToolSpec` 今天只有 `servers` + `allow_tools`**(`agent_spec.py:1089-1102`),没有逐参数配置;且 `model_config = ConfigDict(extra="forbid")` —— **旧版本代码读到新增字段会校验失败**(见 §七回滚)。
5. **沙箱出网策略是活的,不是死字段**。`NetworkSpec`(`agent_spec.py:268-290`):`allowlist` 空 → 任何**公网**主机放行,私网/SSRF 静态闸仍挡,`denylist` 优先级最高,全程审计;策略经 `agent_factory.py:796-800` 签进 egress token,由 credential-proxy 强制。**默认姿态是 allow-all-public,不是「只许列出的域名」**。
6. **run 启动的 config 组装有四处**:`api/runs.py:1246`、`run_queue_worker.py:354`、`trigger_firing.py:286`、`orphan_sweep.py:393`——任何「run 开始时做一件事」的规矩写在那一层必漏(历史上 run trace 绑定就漏过两处)。四条路都必经的单一入口是**图本身**。
7. **平台给沙箱下发自己的代码是现成做法**:CM-0 投影写 `PLAN.md` 就是这么干的(`tools/file_ops.py:885-910` `SandboxWorkspaceWriter`)。
8. **配置页不是 manifest 的唯一入口**:`apps/admin-ui/src/components/manifest-editor/YamlView.tsx` 是 YAML 直编,后端 `api/agents.py:1994` 的 `PUT /{name}/{version}/draft` 直收 manifest。**下拉框挡不住非法值**。
9. **run inputs 已有大小闸**:64 个键、单值 8192 字节(`api/agents.py:789` 一带)。本设计不放宽。

---

## 一、问题(实证)

测试环境会话 `9400f96a-0f17-4ed0-8349-8dfc5218bf0b`(agent `ai-health-plan`,模型 glm-5.3),平台把 `org_logo` 正确注入系统提示词:

```
https://deep-ai-health-test.oss-cn-hangzhou.aliyuncs.com/plan-generator/brand-logo/1789382970142-logo11.jpg
```

模型写 `exec_python` 代码时抄成 `…/17889382970142-logo11.jpg`(多一个 8),平台原样执行 → 404。**三轮三次**:两次时间戳抄错,一次 percent-encoded 中文被改字。模型随后告诉员工「LOGO 404,需要更换地址」,把自己的抄写错误归到对接方头上。

排查判据:`run_event` 的 `system_prompt` 事件 vs `updates` 里的 `tool_calls.args.code` —— 一比就知道是平台改的还是模型抄的。本例平台注入与透传全对。

**根因**:LLM 复制长数字/编码串本质是逐 token 再生成,不是复制;tokenizer 按 3 位一块切数字,`1789`→`17889` 正是块边界复制错。**任何「把值放进提示词、让模型再打进代码」的设计都带这个失效模式**,靠「请仔细抄」的提示词修不了。

---

## 二、目标与非目标

**目标**

- 长不透明值(URL、编码串、长 ID)不再经过模型的「手」——不管它最终去沙箱代码还是去工具参数。
- 平台默认行为,不是 opt-in 开关(开关有人会忘,忘了静默跑偏)。
- 存量 agent 零改动即可跑;收益随对接方改提示词兑现。

**非目标**

- 不改 run inputs 的对外契约(键名、64/8192 上限、`required`/`trusted` 语义全不动)。
- 不强制对接方把内联值从模板里拿掉——平台给路,不拆桥。
- v1 不覆盖 `http` 等内置宿主工具的参数绑定(URL 类的值已经走数据面了),只做 MCP。

---

## 三、总览:两半,按消费者分流

| 变量的消费者 | 走哪条路 | 模型看到什么 |
|---|---|---|
| 沙箱代码(`exec_python` / `bash`) | **数据面**:`inputs.json`(+ 多媒体文件预拉) | 「输入在 `$EXPERT_WORK_INPUTS` 指向的 JSON 里,读它」 |
| 宿主侧工具参数(MCP) | **绑定面**:配置里把参数绑到声明变量 | **什么都看不到**——该参数从给模型的 schema 里删掉了 |

**分流判据写进 spec,是实现时的硬依据**:一个变量往哪走,看它的消费者,不看它长什么样。

**防抄错的那道闸是 `inputs.json` 和 schema 剥字段,不是预拉。** 预拉只解决「沙箱要出网下载」这件次要的事。这个区分决定了失败语义(§4.3):预拉失败不阻断 run。

---

## 四、数据面

### 4.1 位置与结构

落盘(路径相对用户根,`agents/<agent_key>/` 之后就是 B-60 的 exec 视图根 `/workspace`):

```
agents/<agent_key>/inputs/<run_id>/inputs.json
agents/<agent_key>/inputs/cache/<sha256(url)[:32]><ext>
```

**勘误(2026-09-15,T11)**:预拉文件原设计写在 `<run_id>/files/<变量名>.<ext>`,按 run 一份。
改为**内容寻址、按 agent 共享**的 `inputs/cache/`:同一个 URL 跨轮只下一次。理由是同一个病的两面 ——
每用户工作区配额 10 GiB 挂在 `AgentSandboxClient.acquire` 的闸上,20 MB 素材 × 500 轮就到顶,
而按 run 复制既浪费带宽又是这条增长曲线的分子。文件名只由 URL 的 sha256 决定,租户字符串不进路径。
缓存条目 24 小时算新鲜(命中不重下),超期重下 —— 于是 mtime 天然是「最近引用时间」的 24h 粒度近似,
§4.6 的回收闸按它过期。

`inputs.json`:

```json
{
  "run_id": "382f6f5a-55c4-49be-ac05-32fa143f010d",
  "variables": {
    "project_code": {"value": "PRJ001", "trusted": true},
    "org_logo": {
      "value": "https://deep-ai-health-test.oss-cn-hangzhou.aliyuncs.com/plan-generator/brand-logo/1789382970142-logo11.jpg",
      "trusted": true,
      "local_path": "inputs/cache/9f2a4c1b7e0d3856a1f4c920b7d5e386.jpg"
    },
    "materials": {
      "value": [
        {"description": "示范视频", "url": "https://…/a.mp4", "local_path": "inputs/cache/4d17b0e93c5a2f68d0b14e7a92c3f581.mp4"},
        {"description": "参考文章", "url": "https://…/post", "local_path": null}
      ],
      "trusted": false
    }
  }
}
```

规则:

- 每个变量统一是对象,`value` 是原值(结构原样保留),`trusted` 是声明里的值。
- **`local_path` 相对 `/workspace`**,不是绝对路径——代码 `Path("/workspace") / local_path` 或直接相对 cwd 都成立(B-60 的 exec cwd 就是 `/workspace`)。
- 嵌套结构里的 URL,`local_path` 就地挂在那一项上(见 `materials`)。
- 预拉没命中的,`local_path` 为 `null`(键一定在,不要求代码判 `KeyError`)。

### 4.2 写入时序(单阶段,在图的 run-start 节点里)

`inputs.json` 的写入、预拉、回填**一次做完**,位置是图里的一个 run-start 节点,与现有
`workspace_ingest_node`(CM-0 把人改过的 PLAN.md 读回来)同一层,由 `agent_factory` 在
「agent 声明了变量 **且** sandbox runtime 已接」时装上。

两个理由:

1. **宿主侧先写一份是纯冗余**。沙箱起不来时,没有任何代码会去读 `inputs.json` —— 读它的只有
   `exec_python` / `bash`。所以「沙箱失败也要有文件」这个诉求本身不成立。
2. **run 启动的 config 组装有四处**(`api/runs.py:1246`、`run_queue_worker.py:354`、
   `trigger_firing.py:286`、`orphan_sweep.py:393`),在那一层做就是「规矩写一处漏三处」;
   图里的节点是四条路都必经的单一入口。

**没有声明变量的 agent 不装这个节点**——零副作用、零额外 acquire。

### 4.3 预拉规则

**在沙箱里执行,不在宿主侧。** 三条理由,按重要性排:

1. **治理**:预拉是替模型做它本来要做的事,就该受同样的约束。走沙箱自动继承该 agent 的 `sandbox.network` 策略;走宿主等于平台给自己开后门,绕过租户亲手配的出网策略。
2. **安全**:宿主侧(control-plane pod)摸得到数据库、内部服务、云元数据接口,那道私网闸在那边**不存在**,要重造一遍(造漏就是 SSRF 打内网)。沙箱侧是现成的、被 credential-proxy 强制的。
3. **成本**:大文件下完直接落自己的 `/workspace`(NAS 上就是 agent 目录),零拷贝;宿主侧要 GET 进内存再写 NAS。

**判定**:递归扫 `value` 里所有字符串,`http://` / `https://` 开头的就试。按**响应的 content-type** 决定留不留:

| content-type | 处置 |
|---|---|
| `image/*`、`video/*`、`audio/*`、`application/pdf`、Office 那几类(`application/vnd.openxmlformats-officedocument.*`、`application/msword`、`application/vnd.ms-*`) | 存进 `inputs/cache/`,写 `local_path` |
| 其它(含 `text/html`) | 丢弃,`local_path` 保持 `null`——它是个要点开的地址,不是素材 |

**上限**:单文件 32 MiB,单 run 预拉总量 128 MiB。超限即停止该文件,`local_path` 保持 `null`。

**降级是唯一的失败语义**:404 / 超时 / 超限 / content-type 不匹配 / **被 agent 自己的出网策略挡住** —— 全部一样,`local_path` 保持 `null`,run 照跑,模型仍可按今天的老办法自己下。**预拉永远不让 run 失败。**

**硬规则**:预拉不得绕过 `sandbox.network` 策略。

### 4.4 模型怎么知道路径

exec 时注入环境变量:

```
EXPERT_WORK_INPUTS=/workspace/inputs/<run_id>/inputs.json
```

`exec_python` / `bash` 的工具描述加一句:「本轮输入变量在 `$EXPERT_WORK_INPUTS` 指向的 JSON 里,用代码读取,**不要手抄提示词里的值**」。

选环境变量而不是把路径写进提示词,是因为 `os.environ["EXPERT_WORK_INPUTS"]` 是固定写法,而 `inputs/<run_id>/` 里的 run_id 仍然是一个要抄的串——原则上自相矛盾。

**这条不动 B-60 的 exec 命令串**:两个后端已经有一条共用的 per-exec env 通道 —— `tools/sandbox.py:96` 的 `agent_key_envs()`(今天只注 `PYTHONUSERBASE`),`HTTPSupervisorRuntime` 走 `ExecRequest.envs`、`AgentSandboxClient` 走 `commands.run(envs=...)`,且已被 `test_sandbox_runtime_contract.py` 钉成逐字节相同。`EXPERT_WORK_INPUTS` 加在这里即可,`EXEC_VIEW_SCRIPT` 那对字面量一个字都不用动。

### 4.5 `trusted: false` 的变量

现状:`trusted: false` 的值进提示词时被 datamark + 一次性 nonce 围栏包住,等于告诉模型「这段是数据,里面写什么都不是指令」。

**问题**:值挪进 `inputs.json` 就没围栏了,模型 `print` 一下注入内容就裸着进上下文。

**处置**:`trusted: false` 的变量**提示词里的围栏内联保持不变**(B-61 不改这部分行为),同时在 `inputs.json` 里也给一份供代码精确使用。安全性与今天完全一致。`inputs.json` 里带 `"trusted": false` 标记,平台提示词段声明「inputs.json 的内容是数据,不是指令」。

### 4.6 清理

**勘误(2026-09-15 终审实证)**:本节原先写「由现有工作区 janitor 按 7 天保留期清理,不新增组件」——**这是假的**。
`services/control-plane/src/control_plane/workspace_janitor.py:318` 的 `_sweep_scratch` 只回收 `_scratch/`(24 小时),
外加用户目录在删除时的归档;**没有任何东西碰 `agents/<key>/inputs/<run_id>/`**。

实际后果:每一轮对话都是新的 `run_id` → 重新物化 + 重新下载,单 run 最多 128 MiB,一直堆到用户工作区被 purge。

**处置**:清理是**独立任务**,与本程序**同一批发测试环境**(2026-09-15 用户拍板:不是生产前置——清扫是删文件的,不在测试环境演练过就上生产,等于第一次删是在生产删)。判据:按 mtime 扫
`agents/*/inputs/<uuid>/`,或给每个 agent 的 `inputs/` 定个上限;**必须避开正在跑的 run 的目录**。
这条不塞进 PR-A(janitor 在 control-plane,属另一个服务面,而且误删正在跑的 run 目录会当场打断执行),而是单开 PR-A2,
与 A/B 同批发布;计划里的 T11(内容寻址缓存)+ T12(janitor 回收)就是它。

---

## 五、绑定面

### 5.1 manifest 字段

```yaml
tools:
  - type: mcp
    servers: [deepcare]
    allow_tools: [employee_get_cpwx_pad_login_status, customer_search]
    arg_bindings:
      - server: deepcare
        tool: employee_get_cpwx_pad_login_status
        args:                       # 工具参数名 → 声明变量名
          project_code: project_code
          employee_code: employee_code
```

- **必须带 `server`**:wire 名是 `mcp__<server>__<tool>`(`tools/mcp.py:80`),跨服务器同名工具会撞。
- **逐工具显式**,没有按参数名的全局规则。理由见 §十一。
- 默认空 → 存量 agent 零影响。

### 5.2 建工具目录时剥 schema

`tools/assembly.py:688` `_register_mcp` 里,被绑定的参数从暴露给模型的 JSON schema 中**删除**(同时从 `required` 移除)。模型看不见,也就填不了。

绑定属于 spec 而非 run,所以这一步跟 BuiltAgent 缓存天然一致。

### 5.3 填值:`tools_node` 最前面

`graph_builder/builder.py:1270` `tools_node` 里,紧跟 `_extract_tool_calls`(`:1275`)之后,按 `config["configurable"]` 里本轮的 inputs 把绑定参数填回 `tool_call["args"]`。

**为什么不是 `before_tool_dispatch`(`builder.py:2521-2526`,那里也能改写 `tool_args`)**:审批门(`find_approval_target`)和 action screening(`_first_misaligned_action`)都在 dispatch 之前读 args。填在 `tools_node` 最前面,人在审批面板看到的是**真值**、judge 也是对真参数判对齐;填在 dispatch 那层,这两处看到的都是缺参数的调用。

实现成纯函数 `apply_arg_bindings(tool_calls, bindings, inputs) -> list[dict]`,不可变返回,单测直接覆盖。

### 5.4 错误语义

| 情形 | 处置 | 为什么 |
|---|---|---|
| 绑定的变量本轮没传(`required: false`) | 该参数不填 | schema 里也没有这个字段,MCP 服务端按自己的 required 报错给模型,模型能看懂并改道——**不静默** |
| 绑定指向未声明的变量名 | **manifest 校验期拒**(保存草稿 / 发布 dry-run 都会跑) | 下拉框只管住表单那一条路;YAML 直编、`PUT …/draft`、模板复制都绕得过(§零 #8) |
| 删除一个被绑定引用的声明变量 | **拒删**,并列出哪几个工具在用 | 跟删技能 / 删子 Agent 同一口径;不留悬空引用 |
| 绑定的参数在该工具 schema 里不存在(对方改了接口) | 建目录时记一条告警,该条按未命中处理,**不阻断 run** | 上游接口漂移不该让整个 agent 起不来 |

### 5.5 配置页

manifest-editor 的 mcp tab(`components/manifest-editor/groups/CapabilitiesSection.tsx:73-80`)里,勾选的工具可展开,每个参数一行:**「自动(模型填)」/「绑定变量」** 二选一,绑定时下拉列该 agent 的声明变量。

不做批量按钮(用户 2026-09-15 拍板)。新增工具不自动继承绑定——对「同名不同义」正好是一道人工闸。

---

## 六、安全

1. **预拉不新增出网点**(§4.3 已定:在沙箱里跑)。默认姿态是 allow-all-public + 私网静态闸 + denylist,与模型今天自己下载完全一致。
2. **`trusted: false` 保持围栏**(§4.5),安全性不比今天差。
3. **绑定收窄而非放宽**:模型接触不到绑定值,且该值**只能**出现在配置指定的参数位置。今天值在提示词里,模型想塞进任何工具参数都行。
4. **不记值**:审计与日志沿用现有口径——记变量名、结果、字节数,不记值、不记 URL 全文(`api/runs.py:1129` 的 `prompt_var_names` 就是这个口径)。

---

## 七、存量、迁移、回滚

**发版当天不需要对接方做任何事。**

| agent | 会发生什么 |
|---|---|
| 没有声明变量的 | 什么都不变,平台不写 `inputs.json` |
| 有声明变量的 | 工作区多一个 `inputs/<run_id>/`;提示词一个字不改,照常跑(值仍在提示词里) |
| 配了 `arg_bindings` 的 | 只有人工配过才有 |

**收益要等对接方改提示词**(第二步,他们自己挑时间):`ai-health-plan` 模板 5 处——L7/L10 不再内联 `{{ org_logo }}`/`{{ materials }}`、新增「输入文件(硬规则)」段、L63/L65 封面 LOGO 改用 `local_path` 且失败判据改「为空或本地不存在」、L85-87 素材每项用 `local_path`、超链接 URL 必须代码从 inputs.json 读。落进 Agent 配置书 #1235 addendum。

**回滚的坑(必须写进发布清单)**:`MCPToolSpec` 是 `extra="forbid"`,**旧版本读到带 `arg_bindings` 的 manifest 会校验失败**——不是行为退化,是那些 agent 直接起不来。处置:回滚窗口内先别配绑定;或回滚前先清掉绑定配置。与 B-50 那次「回滚窗口在数据搬迁之前」同类。

**无 DB 迁移**:绑定存在 `spec_json`(JSONB)里。`inputs/` 的清理见 §4.6 勘误 —— 现有 janitor **不**管它,由 PR-A2(计划 T11/T12)补上,与本程序同批发测试环境。

---

## 八、可观测性

- **预拉**:每个变量一条——命中/未命中、字节数、耗时、未命中原因(404 / 超时 / 超限 / content-type / 被出网策略挡)。不记值、不记 URL 全文。
- **绑定**:`TOOL_CALL` 审计行加一列「本次调用有哪些参数是平台绑定填的」(参数名)。
- 两者都要能回答同一个问题:**这个值是平台给的,还是模型打的。** 这正是本轮排查 `org_logo` 时缺的东西。

---

## 九、测试

**预拉(五条)**:content-type 白名单命中与未命中;单文件超限;404;超时;**被 agent 的 `sandbox.network` 策略挡住**。全部断言 `local_path is None` 且 run 成功。

**inputs.json**:结构契约(键名、`local_path` 相对语义)钉一份,与文档同源;嵌套结构(`materials` 列表)里 `local_path` 就地挂;没有声明变量的 agent 不写文件。

**绑定(四条)**:模型拿到的 tool catalog 里**确实没有**被绑定的参数(含 `required`);填值发生在**审批之前**(审批请求里看得到真值);悬空引用被 manifest 校验拒;参数不在工具 schema 里时告警且不阻断。

**真栈**:探针 agent 跑一次带 URL 变量 + 一个绑定参数的 run,验 `local_path` 落地在 `agents/<key>/inputs/cache/`、MCP 调用的参数由平台填、模型的 catalog 里没有该参数。**不碰对接方的两个 agent。**

**变异自证**:每条新断言必须 break → red → restore → green(修复自带的测试会给坏版本发合格证)。

---

## 十、PR 切分与波次

| 波 | PR | 内容 | 依赖 |
|---|---|---|---|
| 1 | A | 数据面:run-start 节点(写 `inputs.json` + 沙箱内预拉 + 回填)+ `EXPERT_WORK_INPUTS` 走现有 `agent_key_envs` 通道 + 工具描述 | — |
| 2 | B | 绑定面后端:`arg_bindings` 字段 + manifest 校验 + schema 剥字段 + `apply_arg_bindings` + 审计 | A(两者都动 `sse.py` 的 `configurable` 字面量) |
| 3 | C | 配置页:逐工具逐参数「自动/绑定」 | B |
| 3 | D | 文档:对外文档 + Agent 配置书 #1235 addendum(`ai-health-plan` 5 处) | A、B |

A 与 B 都要动 `sse.py` 的 `configurable` 字面量(A 不需要、B 需要 —— 但 A 先落可以顺手把键加上),所以串行:A → B → (C ∥ D)。

---

## 十一、否决的方案(连理由一起记,以后别回头再论一遍)

1. **`${inputs.x}` 引用语法**(模型在参数里填一个引用名,平台替换):比配置绑定多一层模型参与——模型仍要决定哪个变量填哪个参数,并且要把引用名打对。覆盖的场景(同一参数这次用 A 变量下次用 B)在现有需求里不存在。**降为后续**,真出现动态需求再做。
2. **按参数名的全局绑定规则**(「所有工具的 `project_code` 都绑到变量 X」):配置省事,但**同名不同义时会静默灌错值**——另一个 MCP 服务器的 `project_code` 可能完全不是一回事,而日志里看不出这个参数是谁填的。工具之间不同质,全局规则假设它们同质。
3. **配置页的批量应用按钮**:只是 UI 便利(存储与运行期跟逐工具配一模一样),用户拍板不做。
4. **owner 声明变量的 `kind: url|file|value`**:预拉判定交给 owner 显式标。否掉的理由——防抄错的闸是 `inputs.json` 不是预拉,预拉失败又不阻断 run,所以自动探测猜错的代价只是一次没用的网络请求;而要求 owner 回去标一遍字段,存量 agent 拿不到任何收益。
5. **宿主侧预拉**:见 §4.3 的三条理由。最本质的一条是治理——平台不该绕过租户亲手配的出网策略。

---

## 十二、待定 / 后续

- **服务端把值绑定到内置宿主工具**(`http` 等):v1 不做,URL 类的值已经走数据面。
- **同用户同 agent 并发 run**:本设计按 `inputs/<run_id>/` 分目录 + 环境变量指路,已彻底隔离;工作区里**其它**文件的并发覆盖是老问题,不在本设计范围。
- **预拉的缓存**:同一 URL 在多轮里重复下载。先不做——按 run 隔离比省流量重要。注意这条原先的理由(「7 天保留期内 NAS 占用可观测」)建立在 §4.6 那条被证伪的 janitor 说法上;清理任务落地、占用真能观测之后再定。
