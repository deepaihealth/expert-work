# B-317 v2 「本轮输入」只陈述事实 + 所有工具都认的短路径 设计

> 状态:设计稿,待审查。2026-11-18。
> 范围:四件事一起做 —— ①「本轮输入」段只写事实 ② 一个所有工具都认的短路径 ③ 非代码工具参数里的 `$变量名` 守卫 ④ 两个代码工具的描述。
> 不复用旧分支 `fix/b317-inputs-block-no-needless-file-read` 的分类思路(按 Jinja 语法树判断变量进没进提示词,被审查否掉:误判一大片)。本方案不判断「变量在不在提示词里」,只写平台确定知道的事实。

---

## 一、问题和实测数据

### 1.1 现象

健管师(B-314 迁移的纯对话智能体)在测试环境跑了 154 轮:

- **19 次**跑 `exec_python`,只是为了读清单里 `attachment_urls` 和 `is_first_contact` 的值(ROADMAP 登记时记的是 17 轮)。这个智能体根本不需要代码:`is_first_contact` 只在模板的 `{% if %}` 里用,`attachment_urls` 绑定给了深护 MCP 表单工具,平台自动填。
- **1 次**把字面量 `$attachment_urls` 写进了 MCP 表单工具的参数,对方服务收到的就是这串字。

### 1.2 原因(读代码确认)

1. **「本轮输入」段开头两句是命令。** `services/control-plane/src/control_plane/inputs_block.py:113-117`:
   - 「输入文件目录 $EXPERT_WORK_INPUTS_DIR，清单 $EXPERT_WORK_INPUTS（exec_python / bash 里直接用）。」
   - 「代码里要用到下面任何值时，从目录或清单读；不要从上文手抄，长串抄错一位就是 404。」

   这两句对每个声明了变量的智能体、每一轮都出现。模型读到「要用到下面任何值时从清单读」,又看到 `is_first_contact：已提供（非文件）`,自然就去跑代码读值。第二句的 `$EXPERT_WORK_INPUTS` 写法又教会了它「`$名字` 代表一个值」,于是有了 `$attachment_urls`。
2. **`$EXPERT_WORK_INPUTS_DIR/...` 只有 exec_python / bash 认识。** 它是沙箱里的环境变量(`tools/sandbox.py:142-145`)。系统提示词渲染(`prompt_render.py:118`、`:181`、`:217`)和「本轮输入」段(`inputs_block.py:73`、`:76`、`:85`、`:89`)都把文件写成这种形式,但 `ask_image` 的 `path`(`tools/vision.py:1498-1508`)、`read_document`、`read_page`、文件工具都走 `resolve_workspace_path`(`tools/workspace_paths.py:282-358`),那里没有任何地方展开 `$`;MCP 工具更不可能展开。grep 全仓库也没有展开它的代码。
3. **另一个没人发现的坑(读代码判断,实现第一步先写测试证实)。** 就算模型手写出真实路径 `inputs/<run_id>/org_logo.png` 交给 `ask_image`,也读不到:预下载建的是**符号链接** `org_logo.png -> ../cache/<摘要>.png`(`tools/prefetch_script.py:160-185`),而宿主侧读工作区文件的两条路 —— `NasWorkspaceStore.read_file`(`tools/nas_workspace_store.py:1063-1095`)和 `multimodal._read_workspace_leaf`(`multimodal.py:243`)—— 末段都用 `O_NOFOLLOW` 打开,碰到符号链接按「越界」拒绝。`read_document` / `read_page` 在沙箱里按 `realpath` 读,链接在 `/workspace` 之内,这两个读得到。

### 1.3 现在长什么样

健管师一轮的「本轮输入」段(变量名照 B-314 06 §5.1):

```
[本轮输入]（平台自动生成）
输入文件目录 $EXPERT_WORK_INPUTS_DIR，清单 $EXPERT_WORK_INPUTS（exec_python / bash 里直接用）。
代码里要用到下面任何值时，从目录或清单读；不要从上文手抄，长串抄错一位就是 404。
- customer_code：已提供（非文件）；绑定了它的工具由平台自动填，不用手抄
- attachment_urls：已提供（非文件）；绑定了它的工具由平台自动填，不用手抄
- is_first_contact：已提供（非文件）
……
```

---

## 二、业界做法

| 做法 | 出处 | 我们对应的地方 |
|---|---|---|
| 平台已知的值不交给模型:工具参数从 schema 里删掉,调用时注入 | LangChain `InjectedToolArg` / `InjectedState`;我们自己的 `arg_bindings`(`orchestrator/tools/arg_bindings.py:212`) | 已经这么做,不改。本设计只让「本轮输入」段对它说一句事实,不再说命令 |
| 附件只陈述事实:有文件说一句「文件在 X」,没有就不出现 | deer-flow `<uploaded_files>`(`~/src/github/deer-flow/backend/packages/harness/deerflow/agents/middlewares/uploads_middleware.py:194-251`);openclaw `[media attached: media://inbound/<名>]`(`~/src/github/openclaw/src/auto-reply/media-note.ts:30-45`);ADK 用 artifact 名指代文件(用户调研结论) | 「本轮输入」段 |
| 一个固定的虚拟路径,所有工具经同一个函数解析到真实目录 | deer-flow 的 `/mnt/user-data/uploads/<文件>`:`VIRTUAL_PATH_PREFIX`(`deerflow/config/paths.py:11`),文件工具走 `replace_virtual_path`(`deerflow/sandbox/tools.py:652`),本地沙箱命令走 `replace_virtual_paths_in_command`(`:1219`),容器沙箱靠挂载 | 第 2 点的 `inputs/current/` |
| 「代码里从清单读、别手抄」这类用法说明只放在代码工具的描述里 | 同上各家:用法写在工具描述,不写在每轮上下文 | 第 4 点 |

结论:本方案就是把这三条照搬到我们的结构上 —— 段里只写事实;一个短路径所有工具都认;命令只留在代码工具描述里。

---

## 三、四点方案

### 3.1 第 1 点:「本轮输入」段只陈述事实

**改什么**:`services/control-plane/src/control_plane/inputs_block.py`

1. 删掉开头两句命令(`:115-116`)。
2. 有文件时加一句事实。「有文件」= 本轮传了值的变量里,至少有一个被 `linked_sites` 认出文件(与预下载同一个判定,`orchestrator/tools/inputs_doc.py:248`)。没有文件就不出现这一句。
3. 每个变量一行,格式不变:`- 名字（说明）：状态`。状态全部改成事实,见 §四 的逐字对照。
4. 被绑定的变量:写「由平台填进绑定它的工具参数」,**不列工具名**。理由:
   - 业界做法是被注入的参数模型根本看不见(LangChain `InjectedToolArg`)。我们之所以还要说一句,只是因为变量名本身出现在这一段里,模型需要知道「这个值不归你传」。
   - 列工具名对模型没用(那些参数已经从 schema 里删了,模型看不到也填不了),却每轮多几十个 token:健管师 8 个身份变量各绑 3~10 个工具,列出来每轮多二三百 token,这段还会留在会话里逐轮累积。
   - 真写错时,守卫(第 3 点)的拦截提示会点名「本工具的哪个参数由平台填」,那时才是需要具体信息的时候。
   - 这是一个需要你定的点(§十二 第 2 条)。
5. 「本轮未提供」优先(裁定 P12 不变):可选变量被绑定但本轮没传,只写「本轮未提供」。
6. **零值原则不变**:段里不写任何值;untrusted 变量的真实文件名(扩展名来自租户 URL)不写,列表项说明、字典键用占位符。

**不变的东西**(实现时不碰,测试钉住):

- 消息构造 `inputs_block_message`、打标 `INPUTS_BLOCK_MARK`(`inputs_block.py:138-148`)不变 → regenerate 重放(`supersede.py:410-419` 按标认段)照旧带走它;压缩后 `_keep_latest_inputs_block`(`graph_builder/builder.py:2799`)照旧放回最新一段。
- `block_stats` 三个计数(`inputs_block.py:125-135`)语义不变:`variable_count` / `bound_count` / `url_count`。
- 生成时机和位置不变(`api/runs.py:596-610`,只在 jinja 智能体上、贴在用户消息之后)。
- 说明文字压一行、限 40 字不变。

### 3.2 第 2 点:所有工具都认的短路径 `inputs/current/`

#### 3.2.1 选哪个路径

**选 `inputs/current/<文件>`**,不选扁平的 `inputs/<文件>`。

`inputs/` 底下现在已经有东西,而且是契约:

```
inputs/
  <run_id>/            ← 每轮一个,inputs.json + 按变量名建的符号链接
    inputs.json
    org_logo.png -> ../cache/<摘要>.png
    materials/0-示范视频.mp4 -> ../../cache/<摘要>.mp4
  cache/               ← 按 URL 摘要的共享缓存
```

扁平的 `inputs/<文件>` 有三个过不去的坎:

1. **并发会串。** 同一个 `/workspace` 可能被多个 run 同时用:老视图(B-236 之前开的对话)里是整个智能体根,同一用户的多个对话共用;对话视图里,审批挂起的旧一轮续跑时,新一轮可能已经写过。把链接平铺在 `inputs/` 下,后写的覆盖先写的,续跑的那一轮会读到别一轮的文件。
2. **符号链接会断。** 想在沙箱里用挂载把本轮目录「盖」在 `/workspace/inputs` 上,链接 `../cache/x` 就会指到 `/workspace/cache/x`,不存在。
3. **`local_path` 契约会断。** 清单里 `local_path` 是 `inputs/<run_id>/org_logo.png`(相对 `/workspace`),对外文档(`apps/admin-ui/docs-site/guide/chat.md:558`、`:685-689`)教第三方这么用,盖住 `inputs/` 这些路径全部失效。

`inputs/current/` 和 `<run_id>/` 在同一层,三个坎都没有:

- 每次 exec 在自己的挂载命名空间里把**本轮的** `inputs/<run_id>/` 挂到 `inputs/current/`(§3.2.3),并发的两个 exec 各看各的,不串。
- 挂载点和 `<run_id>/` 同深度,`../cache/x` 解析到 `inputs/cache/x`,链接照常可用(嵌套的 `../../cache/x` 同理)。
- `inputs/<run_id>/...`、`inputs/cache/...`、两个环境变量的值一个字不改。

**名字 `current` 不会撞**:run 目录名是 UUID,缓存目录叫 `cache`,回收任务只收 UUID 名的目录(`control_plane/workspace_janitor.py:220`、`:231`),不会碰它;变量叫 `current` 也没事,它的链接在 `inputs/<run_id>/current…` 里,不在 `inputs/` 这一层。`inputs/` 本来就是保留前缀(`expert_work/persistence/workspace/layout.py:266-268`),控制台文件浏览和工作区快照都不列它。和 B-236/B-262 的布局:对话视图里 `inputs/` 是本对话 `threads/<对话>/work/` 下的普通子目录,老视图里在智能体根下,`inputs/current/` 跟着 `inputs/` 走,不碰 `threads/`、`uploads/`、`.tool_results/`。

**「current」的意思**:永远是「这一轮的输入」,和今天 `$EXPERT_WORK_INPUTS_DIR` 的语义完全一样(那个环境变量也只指本轮)。上一轮的文件仍在 `inputs/<上一轮 run_id>/`,但平台不再指给模型 —— 和今天一样。

名字是否用 `current`,是需要你定的点(§十二 第 1 条;候选还有 `this_turn`、`turn`)。

#### 3.2.2 平台工具:一个解析函数

**改 `orchestrator/tools/workspace_paths.py`**:

1. `WorkspaceView`(`:172-199`)加一个字段 `inputs_run: UUID | None = None` —— 本轮输入目录属于哪个 run。
2. `ToolContext.workspace_view`(`tools/registry.py:456-463`)里填它:`(inputs_run_id or run_id) if prompt_inputs else None`。这正是 `run_in_sandbox` 今天给环境变量用的那个表达式(`tools/sandbox.py:962`),改成两边都从 `view.inputs_run` 取,保证 exec 的挂载、环境变量、宿主侧解析用的是**同一个值**(与 B-236「本次 exec 的视图与宿主侧解析路径用同一个值」同一个口径)。
3. `resolve_workspace_path`(`:282-358`)在折叠 `/workspace/` 之后、拆段之前加一步:
   - 头两段是 `inputs` + `current` → 改写成 `inputs/<inputs_run>/<余下>`。`ResolvedPath.rel` 是改写后的真实路径,`ResolvedPath.path`(给模型看、可回传的写法)保留模型写的短路径。
   - `inputs_run` 为 `None`(本轮没有输入变量)→ 报错:「`{tool}`: inputs/current/ 是本轮输入文件的目录；本轮没有输入变量，里面没有文件: '{path}'」。
   - **兼容别名**:开头是 `$EXPERT_WORK_INPUTS_DIR/` 或 `${EXPERT_WORK_INPUTS_DIR}/` 时,当成 `inputs/current/` 处理。理由见 §八(旧会话里的旧段、租户模板里写的旧说法)。
   - `/agent/inputs/current/...` **不改写**:沙箱里只在 `/workspace` 那一侧挂载(老视图里 `/agent/inputs/current` 是个空桩),宿主侧和沙箱侧要说同一句话。对话视图里 `/agent/inputs/` 本来就拒(`:344-351`),不变。
   - `inputs/<run_id>/...`、`inputs/cache/...`、`inputs/currentx/...` 都不改写,与今天相同。
4. **为什么 `rel` 要换成真实路径**:`read_page` 的渲染目录按 `rel` 算 `doc_sha`(`tools/read_page.py:1313-1317`)。如果 `rel` 停在 `inputs/current/report.pdf`,第二轮换了一份同名报告,两轮会共用一个 `doc_sha`、读到上一轮渲染的页。换成 `inputs/<run_id>/report.pdf` 就每轮各算各的。

走这个函数的工具(都已经在用,不用逐个改):文件工具(`tools/file_ops.py:170`)、`read_document`(经 `_require_path`)、`read_page`(`read_page.py:1313`)、`ask_image`(`vision.py:1506`)、`save_artifact`(`artifact.py:142`)。

**符号链接那一步(只给宿主侧直读的工具)**:`ask_image` 读工作区图走宿主侧、拒绝符号链接(§1.2 第 3 条)。在 `NasWorkspaceStore` 加一个窄函数 `resolve_input_link(tenant_id, user_id, path)`:

- 只处理 `…/inputs/<UUID>/…` 底下的末段;
- 在已钉住的父目录 fd 上 `readlinkat`(不跟随),目标按链接所在目录归一化后必须**恰好**是同一个 `inputs/` 下的 `cache/<一段文件名>`,且文件名以 32 位十六进制开头(预下载缓存的命名,`prefetch_script.py` 的 `cache_digest`);
- 满足就返回缓存文件的路径,不满足(普通文件、别的链接、目标越界)原样返回,后面照旧按 `O_NOFOLLOW` 读 —— 越界链接仍然被拒。

`ask_image._workspace_image`(`vision.py:1498-1508`)在拼引用前调它一次。这不放宽安全边界:能被认的只有指回本工作区缓存目录的链接,而沙箱代码本来就能读那个文件。`read_file` 不接:预下载只收图片、音视频、PDF、Office(`prefetch_script.content_type_ok`),`read_file` 只读文本,用不上。非 NAS 的存储实现(本地后端)默认原样返回。

#### 3.2.3 沙箱里的 exec_python / bash:每次 exec 挂一次

**改 `orchestrator/tools/exec_view.py`** 和它在镜像里的逐字副本 **`infra/sandbox-image/runner.py`**(契约测试 `test_exec_view_script_matches_the_sandbox_image` 钉两份相等):

1. 脚本多一个位置参数 `$7` = 本轮输入的 run id(空串 = 不挂)。`shift 6` → `shift 7`(`exec_view.py:128`、`runner.py:80`)。shell 部分按对话 id 的写法先校验:只收 `str(uuid)` 形状(36 位小写十六进制加横杠),否则 98 退出(参数不合法是平台的错,失败即失败)。
2. 内嵌 python 的 `nas_view`(`exec_view.py:317-366`)在 `/workspace` 绑好之后加一步,全程按 fd(与现有挂载同一套做法):
   ```
   inputs = sub(ws, "inputs", True)          # O_NOFOLLOW 打开,没有就建
   run    = sub(inputs, run_id, False)       # 必须已存在且是真目录
   stub   = sub(inputs, "current", True)     # 挂载桩,没有就建
   bind(run, stub)                           # open_tree + move_mount,记进 OURS
   ```
   任何一步失败(run 目录不在、`current` 被换成了符号链接或普通文件)→ `say(...)` 写一行 stderr,**这次不挂,exec 照常跑**。理由和 `hide_platform` 一样:一个被种下的普通文件不该让这个智能体之后每次 exec 都失败;这时环境变量 `$EXPERT_WORK_INPUTS_DIR` 仍然可用。
   - 挂载是可写的,和今天 `inputs/<run_id>/` 可写一样(预下载要往里写),能力不比今天多。因为记进了 `OURS`,`freeze()` 不会把它改成只读。
   - 载荷跑在嵌套 user namespace 里,卸不掉这个挂载(与现有各层同理)。
   - 只挂 `/workspace` 一侧,不挂 `/agent`。
   - 没绑智能体(`root` 为空,只有合成评测路径)时不挂。
3. `exec_view_argv` / `build_exec_command`(`:580-638`)加参数 `inputs_run_id`,放在 `$7`;ACS 后端调用处(`tools/agent_sandbox.py:1693`)和本地后端的请求体(`runner.py:637-643` 读请求、`:513-560` 拼 argv)各传一份。值就是 `run_in_sandbox` 已经在传的 `run_id=inputs_run_id`(`sandbox.py:972`),改成取 `view.inputs_run`。

**生效范围**:ACS(测试、生产)的脚本和 argv 都是 orchestrator 每次随命令发过去的(`build_exec_command`),随 orchestrator 发布立即生效,**不用重建沙箱镜像**。本地后端的脚本和 argv 都由镜像里的 `runner.py` 自己拼,orchestrator 只是在请求体里多带一个 `inputs_run_id` 字段:老镜像忽略这个字段、照旧按 6 个参数拼,行为与今天相同(没有挂载,环境变量照旧);重建本地沙箱镜像后才有挂载。新镜像收到不带这个字段的请求时 `$7` 传空串。两种组合都不会错配(§十一 风险 2)。

#### 3.2.4 委派出去的子代

不需要新通道。子代的 `inputs_run_id` 和 `prompt_inputs` 已经从父那里带下来(`tools/_child_run.py:1017-1019`、`:1048-1049`,字段说明 `registry.py:396-424`),于是子代的 `view.inputs_run` = 父那一轮首段的 run id:

- 子代 exec 挂的是父那一轮的目录;
- 子代宿主侧工具解析到同一个目录;
- 对话视图里子代的 `/workspace` 就是父对话的 `work/`,老视图里是同一个智能体根,`inputs/<父 run_id>/` 都在。

#### 3.2.5 系统提示词里的路径

**改 `control_plane/prompt_render.py`** 三处,`$EXPERT_WORK_INPUTS_DIR/{link}` → `inputs/current/{link}`:

- `_render_items`(逐项块,`:118`)
- `_render_url`(整个值是地址,`:181`)
- `render_value` 里 trusted 真 list / dict 的结构副本(`:217`,模板里 `{% for %}` / `{{ x.url }}` 拿到的值)

尾注 `URL_NOTE`(`:43`)同时改成事实,逐字见 §四。

`render_value` 只做字符串替换、不发网、不读盘这一点不变;名字仍由 `inputs_doc.link_names` 决定,与预下载建出的链接逐字相同。

#### 3.2.6 还有谁依赖 `$EXPERT_WORK_INPUTS_DIR` / `$EXPERT_WORK_INPUTS`(全仓库 grep)

| 位置 | 用途 | 处理 |
|---|---|---|
| `tools/sandbox.py:106-146` `agent_key_envs` | 往沙箱注入两个环境变量 | **保留,值不变** |
| `tools/sandbox.py:1107-1121`、`tools/bash.py:98-112` | 代码工具描述 | 改(第 4 点) |
| `control_plane/inputs_block.py`、`prompt_render.py:40-41` | 段、提示词 | 改成短路径;两个常量若不再被引用就删(本改动造成的孤儿) |
| `graph_builder/input_url_guard.py:240-274` | 手抄 URL 守卫的提示 | 改成短路径(只在代码工具里出现,两种写法都对,统一说法) |
| `inputs_doc.py`、`registry.py`、`_child_run.py`、`agent_sandbox.py:1639`、`builder.py`、`workspace_janitor.py`、`turn_inputs.py`、`agent_factory.py:1783` | 注释 | 只改明显说错的注释,不动行为 |
| `apps/admin-ui/docs-site/guide/chat.md:558`、`:657`、`:685-689` | 对外文档 | 加 `inputs/current/` 的说法;环境变量那段保留(仍然有效) |
| `apps/admin-ui/src/components/console/__tests__/PlatformRows.test.tsx:15-21` | 控制台原样显示隐藏行的测试样例 | 不用改:组件逐字显示,不解析 |
| `manifests/`、`configs/`、`platform-skills/`、`environments/` | — | 没有命中 |

### 3.3 第 3 点:守卫

#### 3.3.1 拦什么

一次工具调用,同时满足下面三条就**不派发**,回一条错误 ToolMessage:

1. 工具不是 `exec_python` / `bash`(`builder.py:4349-4352` 的 `_SANDBOX_CODE_ARGS`)。
2. 在它的参数里(递归看所有字符串叶子,不看字典键),**除去**:
   - 这次被平台绑定填过的参数(`_fill_bound_args` 返回的逐调用参数名,`builder.py:1902-1927`)—— 那里是租户真实值,不是模型写的;
   - 写文件工具的「文件内容」参数(`write_file` 的内容、`edit_file` 的新旧文本)—— 写出来的可能是之后在沙箱里跑的脚本,`$EXPERT_WORK_INPUTS_DIR`、同名 shell 变量在那里是合法的;
   - 工作区路径参数(`spec.path_args` 和 `ask_image.path`)里**开头**的 `$EXPERT_WORK_INPUTS_DIR/` —— 解析函数认它(§3.2.2 兼容别名)。
3. 出现下面任一种字面量:
   - `$名字` 或 `${名字}`,其中「名字」是 `$` 后面**完整的**标识符(`[A-Za-z_][A-Za-z0-9_]*`,贪婪取完),且**恰好等于**一个声明过的变量名(区分大小写);
   - `$EXPERT_WORK_` 或 `${EXPERT_WORK_` 开头的标识符。

「声明过的变量名」= 本图 spec 声明的变量名(`agent_factory.py:1379-1382` 旁边同样的写法,新增一个 `declared_input_names`)∪ 本次 config 里 `prompt_inputs` 的键(子代的图不一定带父的声明,但 `prompt_inputs` 是从父带下来的)。

#### 3.3.2 接在哪

照 `graph_builder/input_url_guard.py` 的写法和接入点:

- 新模块 `graph_builder/input_literal_guard.py`,判定是纯函数:`find_variable_literal(args, *, names, skip_args) -> LiteralHit | None` 和 `literal_guard_message(hit, ...) -> str`。
- 在 `tools_node` 里紧挨着 `_guard_sandbox_calls`(`builder.py:2127`)算一次 `literal_hits`,派发时(`:2218` 附近)与 `guard_hits` 同样处理:命中的那条不派发,其余照常。
- 和 URL 守卫一样**放在审批门之后**、只影响派发,不改审批门和 action screening 的判定与下标(裁定 5 的口径);审批续跑重算一遍,判定是确定的。
- 拦下时:审计一行 `tool:blocked`,`reason=input_variable_literal`,details 只记变量名(或环境变量名)和参数名,不记任何值;计数 `outcome="blocked"`;错误分类 `invalid_arguments`(「改参数,别原样重发」)。
- 守卫永不让 run 失败:判定里出任何意外,放行整批、只记异常类型(与 URL 守卫 `builder.py:4265-4280` 同一条规矩)。

#### 3.3.3 回给模型的话(逐字)

`{arg}` 是参数名,`{var}` 是变量名,`{tool}` 是工具名,`{param}` 是绑定参数名 —— 全是配置或 schema 里的名字,不是值,也不来自租户数据,可以直接写。

| 命中 | 消息 |
|---|---|
| `$变量名`,且这个变量绑定在**本工具**的某个参数上 | `[blocked] 参数 {arg} 里的 ${var} 不会被换成值，收到的就是这串字。{var} 由平台自动填进本工具的 {param} 参数（它不在你看到的参数表里），这里不用写，去掉后重新调用。` |
| `$变量名`,MCP 工具或 `http`(对方是外部服务) | `[blocked] 参数 {arg} 里的 ${var} 不会被换成值，对方服务收到的就是这串字。请写出要传的实际内容；如果 {var} 由平台填进绑定它的工具参数，就不用写。` |
| `$变量名`,其它平台工具 | `[blocked] 参数 {arg} 里的 ${var} 不会被换成值。要用 {var} 的文件，写 inputs/current/ 下的路径（见「本轮输入」段）；其它情况请写出实际内容。` |
| `$EXPERT_WORK_INPUTS_DIR` / `$EXPERT_WORK_INPUTS` | 平台工具:`[blocked] 参数 {arg} 里的 ${name} 只在 exec_python / bash 里有值。要用本轮输入文件，写 inputs/current/ 下的路径。` MCP / http:`[blocked] 参数 {arg} 里的 ${name} 只在 exec_python / bash 里有值，对方服务读不到沙箱里的文件。` |
| `$EXPERT_WORK_SKILLS_DIR` | `[blocked] 参数 {arg} 里的 ${name} 只在 exec_python / bash 里有值。读技能文件用 skill_view。` |
| 其它 `$EXPERT_WORK_*` | `[blocked] 参数 {arg} 里的 ${name} 只在 exec_python / bash 里有值。` |

一次调用多处命中,只报第一处(按参数在 args 里的顺序,深度优先)。

### 3.4 第 4 点:代码工具描述

**改 `tools/sandbox.py:1107-1121`、`tools/bash.py:98-112`**:两处今天是两份逐字相同的字面量。改成一个共享常量 `INPUTS_CODE_NOTE`(放在 `tools/inputs_doc.py`,与目录、文件名常量放一起),两个工具都引用它 —— 一致性由结构保证,测试再钉一次。

保留的保护:清单在 `$EXPERT_WORK_INPUTS`、代码里从清单读、不要手抄长串、文件不在就按原地址下载、链接命名规则。改的只有路径说法:文件目录写 `inputs/current/`,注明和 `$EXPERT_WORK_INPUTS_DIR` 是同一个目录。逐字见 §四。

---

## 四、新旧文案对照(逐字)

### 4.1 「本轮输入」段

| 位置 | 旧 | 新 |
|---|---|---|
| 标题 `HEADER` | `[本轮输入]（平台自动生成）` | 不变 |
| 第 2 行 | `输入文件目录 $EXPERT_WORK_INPUTS_DIR，清单 $EXPERT_WORK_INPUTS（exec_python / bash 里直接用）。` | **删除** |
| 第 3 行 | `代码里要用到下面任何值时，从目录或清单读；不要从上文手抄，长串抄错一位就是 404。` | **删除** |
| 有文件时的一句(新增) | — | `本轮输入文件在 inputs/current/ 下（预先下载失败的不在）。` |
| 没传 `_UNSET` | `本轮未提供` | 不变 |
| 非文件 trusted `_TEXT` | `已提供（非文件）` | `已提供` |
| 非文件 untrusted `_UNTRUSTED_TEXT` | `已提供（非文件，外部数据，需逐字使用时从清单读）` | `已提供（外部数据）` |
| 整个值是地址,trusted | `文件 $EXPERT_WORK_INPUTS_DIR/{link}；不在则按清单里的原地址下载` | `文件 inputs/current/{link}` |
| 整个值是地址,untrusted | `$EXPERT_WORK_INPUTS_DIR 下以 {var} 开头的文件（精确文件名读清单里 {var} 的 local_path）；不在则按清单里的原地址下载` | `文件在 inputs/current/ 下，名字以 {var} 开头（确切文件名见清单里 {var} 的 local_path）` |
| 列表 | `{N} 项，每项的文件在 $EXPERT_WORK_INPUTS_DIR/{var}/<下标-说明>；不在则按清单里该项的原地址下载` | `{N} 项，其中 {M} 个文件在 inputs/current/{var}/ 下（按 <下标-说明> 命名）` |
| 对象 | `{M} 个文件在 $EXPERT_WORK_INPUTS_DIR/{var}.<字段名>；不在则按清单里该字段的原地址下载` | `{M} 个文件在 inputs/current/ 下（按 {var}.<字段名> 命名）` |
| 被绑定(接在状态后) `_BOUND` | `；绑定了它的工具由平台自动填，不用手抄` | `；由平台填进绑定它的工具参数` |

说明:「不在则按清单里的原地址下载」从每行挪走了 —— 段里只说一次「预先下载失败的不在」(事实),怎么补救写在代码工具描述里(那是唯一能补救的地方)。untrusted 的那行仍指向清单的 `local_path`:真实文件名带租户 URL 的扩展名,不能进段(裁定 P16),而声明了 `org` 和 `org_logo` 时只说前缀有歧义(B-73 ④)。

健管师改完后一轮的样子:

```
[本轮输入]（平台自动生成）
- customer_code：已提供；由平台填进绑定它的工具参数
- attachment_urls：已提供；由平台填进绑定它的工具参数
- is_first_contact：已提供
……
```

文档分析类(trusted `report` 是 PDF 地址,`materials` 是带地址的列表):

```
[本轮输入]（平台自动生成）
本轮输入文件在 inputs/current/ 下（预先下载失败的不在）。
- report（客户体检报告）：文件 inputs/current/report.pdf
- materials（素材）：3 项，其中 3 个文件在 inputs/current/materials/ 下（按 <下标-说明> 命名）
```

### 4.2 系统提示词渲染

| 位置 | 旧 | 新 |
|---|---|---|
| 路径 | `$EXPERT_WORK_INPUTS_DIR/{link}` | `inputs/current/{link}` |
| 尾注 `URL_NOTE` | `（已就位；不在则按输入清单里的原地址下载）` | `（本轮输入文件；预先下载失败时不在）` |

例:`机构 LOGO 下载地址：{{ org_logo }}` → 旧 `机构 LOGO 下载地址：$EXPERT_WORK_INPUTS_DIR/org_logo.png（已就位；不在则按输入清单里的原地址下载）`;新 `机构 LOGO 下载地址：inputs/current/org_logo.png（本轮输入文件；预先下载失败时不在）`。

### 4.3 exec_python / bash 描述中关于输入的那一段

旧(两处相同):

> 本轮如果有输入变量，它们在 $EXPERT_WORK_INPUTS 指向的 JSON 文件里（含 URL、编码、本地文件路径）；本轮没有输入变量时这个文件不存在，读不到就照上文办，不必重试。需要用到某个输入值时，用代码读这个文件，不要从上文手抄——长串抄错一位就是 404。文件里 local_path 非空表示平台已把该文件下载到本地，直接用它，不必再联网下载；极少数情况下（挂起很久之后续跑）该文件已被清理，这时改用同一项里的原始 URL 自己下载。平台预先下载的文件在 $EXPERT_WORK_INPUTS_DIR 目录里按变量名放着：整个值是地址的变量叫 <变量名><扩展名>，对象字段里的叫 <变量名>.<字段名><扩展名>，列表项叫 <变量名>/<下标-说明><扩展名>；列表项的确切路径在清单里该项的 local_path（值是 JSON 字符串时在 value_parsed[i].local_path）。文件不在就按清单里的原地址下载。地址一律从清单读或用这些文件，不要手抄。

新(两处引用同一个常量):

> 本轮如果有输入变量，它们在 $EXPERT_WORK_INPUTS 指向的 JSON 清单里（含原地址、编码、本地文件路径）；本轮没有输入变量时这个文件不存在，读不到就照上文办，不必重试。代码里要用到某个输入值时，从清单读，不要从上文手抄——长串抄错一位就是 404。平台预先下载的文件在 inputs/current/ 下（与 $EXPERT_WORK_INPUTS_DIR 是同一个目录）：整个值是地址的变量叫 <变量名><扩展名>，对象字段里的叫 <变量名>.<字段名><扩展名>，列表项叫 <变量名>/<下标-说明><扩展名>；确切路径见清单里该项的 local_path（值是 JSON 字符串时在 value_parsed[i].local_path）。文件不在（下载失败，或挂起很久后已被清理）就按清单里的原地址下载。地址一律从清单读或用这些文件，不要手抄。

### 4.4 手抄 URL 守卫的提示(`input_url_guard.py:240-274`)

| | 旧 | 新 |
|---|---|---|
| trusted | `这个文件应在 $EXPERT_WORK_INPUTS_DIR/{link}（不在则按清单里的原地址下载）；请改用它，或用代码从 $EXPERT_WORK_INPUTS 清单里读 {var} 的原地址，不要手抄。` | `这个文件应在 inputs/current/{link}（不在则按清单里的原地址下载）；请改用它，或用代码从 $EXPERT_WORK_INPUTS 清单里读 {var} 的原地址，不要手抄。` |
| untrusted | `这个文件应在 $EXPERT_WORK_INPUTS_DIR 下，确切文件名见 $EXPERT_WORK_INPUTS 清单里 {var} 对应条目的 local_path（不在则按清单里的原地址下载）；请用代码从清单里读路径或原地址，不要手抄。` | `这个文件应在 inputs/current/ 下，确切文件名见 $EXPERT_WORK_INPUTS 清单里 {var} 对应条目的 local_path（不在则按清单里的原地址下载）；请用代码从清单里读路径或原地址，不要手抄。` |

这条守卫只拦 exec_python / bash 的代码,那里命令式的话是对的,只改路径说法。

### 4.5 新守卫和解析报错

见 §3.3.3 的表和 §3.2.2 第 3 条。

---

## 五、跨场景对照表

| # | 场景 | 改完后模型看到什么 / 发生什么 | 为什么不会坏 |
|---|---|---|---|
| a | 纯对话,用了短编号(健管师:身份编号绑给 MCP,`is_first_contact` 进 `if`) | 段里每个变量「已提供」,被绑的加「由平台填进绑定它的工具参数」;没有文件句,没有任何命令。代码工具描述里那段只在模型**打算写代码时**才相关 | 编号的值照旧由 `arg_bindings` 填进工具(`arg_bindings.py:212`,不改);模板渲染非地址值不改;段里不再出现「要用值就去读清单」,模型没有被引去跑代码的理由。万一仍把 `$customer_code` 写进参数,守卫拦下并说明由平台填 |
| b | 文档分析:模板渲染了上传文件的链接,要用代码处理 | 提示词里是 `inputs/current/report.pdf（本轮输入文件；预先下载失败时不在）`,段里是「文件 inputs/current/report.pdf」 | 同一个短路径:exec_python `open("inputs/current/report.pdf")` 经挂载打开;`read_document` / `read_page` 经解析函数改写成 `inputs/<run>/report.pdf`;`ask_image` 再经链接解析读到缓存文件。下载失败时代码工具描述说按清单原地址下载;`$EXPERT_WORK_INPUTS_DIR` 仍可用 |
| c | 附件只绑给工具 | 段里:是地址字符串就「文件 inputs/current/x.jpg；由平台填进绑定它的工具参数」,是裸字符串数组(健管师 `attachment_urls`,不预下载,`inputs_doc.py:317-352`)就「已提供；由平台填进…」 | 绑定填值不改;文件那句是事实,不是要求;工具参数已从 schema 删掉,模型填不了也不必填;写了 `$x` 守卫拦 |
| d | 附件绑给工具,同时要在代码里分析 | 段里同 c;代码里用 `inputs/current/x.jpg` 或读清单 | 两条路互不影响:绑定走 `prompt_inputs`,代码走挂载 / 清单,都指同一轮 |
| e | untrusted 文本 | 提示词里照旧围栏;段里「已提供（外部数据）」;untrusted 地址只说「名字以 {var} 开头」 | 段里仍不写值,也不写由值推出的名字(零值测试钉住);守卫的提示只含变量名、参数名、工具名,都是配置不是数据,不必围栏 |
| f | 用循环渲染链接列表(trusted 真 list) | `{% for m in materials %}{{ m.url }}` 每项得到 `inputs/current/materials/0-示范视频.mp4` | 结构副本只换了路径前缀,下标、遍历、`length` 不变(`prompt_render.py:132-157`);沙箱里挂载点与 `<run_id>/` 同深度,嵌套链接 `../../cache/x` 照常解析 |
| g | 变量只在 if 条件里出现 | 段里「已提供」 | 非地址值的模板上下文不变,`{% if is_first_contact == "1" %}` 判断照旧;本方案不去判断「变量进没进提示词」,所以没有误判可言。剩下的风险是模型出于好奇去读值,见 §十一 风险 5 |
| h | 非 jinja 模板 | 无段;不收 inputs(`prompt_render.py:275-278`,422) | 没有 inputs → 不写清单、不注入环境变量、`$7` 为空不挂载;代码工具描述里那段以「本轮如果有输入变量」开头,本来就是条件句;守卫只剩 `$EXPERT_WORK_` 一条,在非代码工具里写它本来就不会生效 |
| i | 可选变量本轮没传 | 「本轮未提供」(被绑也是,P12) | 模板拿空串(不改);绑定把参数删掉(不改)。若所有变量都没传 → 本轮没有清单 → `inputs_run` 为 `None`:解析函数对 `inputs/current/` 报「本轮没有输入变量」,沙箱里不挂,`inputs/current/` 是空桩,打开即「不存在」 |
| j | 委派的子代 | 子代没有自己的段(段只由入口生成);父可以在任务里写 `inputs/current/x` | 子代 `view.inputs_run` = 父那一轮首段(§3.2.4),exec 挂载和宿主侧解析都到父那份;守卫用 `prompt_inputs` 的键兜住子代图里没有的声明 |
| k | 没有任何输入变量 | 无段 | `build_inputs_block` 返回 `None`(不改);其余同 h |
| l | 模板里自己写了 `$EXPERT_WORK_INPUTS_DIR` | 提示词原样带着这串(租户文字不改写) | 代码工具里环境变量值不变,照旧可用;交给 `ask_image` / `read_document` 等路径参数时,解析函数把开头的 `$EXPERT_WORK_INPUTS_DIR/` 当别名;写进 MCP 参数时守卫拦,提示说对方读不到沙箱文件 |
| m | 审批续跑 / regenerate / 压缩 | 续跑的 `inputs_run_id` 指回首段(`turn_inputs.py`);段按标重放、压缩后放回 | `inputs_run` 跟着 `inputs_run_id` 走,挂载和解析仍是首段的目录;段的标记和构造不改 |

---

## 六、守卫的误伤分析

| 文本 | 拦不拦 | 理由 |
|---|---|---|
| `$100`、`$5.99`、`US$20`、`$ 100` | 不拦 | `$` 后面不是字母或下划线,取不出标识符 |
| `$USD`(没有叫 `USD` 的变量) | 不拦 | 只认声明过的名字 |
| 声明了 `customer_code`,文本里 `$customer`、`$customer_code_v2`、`$Customer_code` | 不拦 | 标识符贪婪取完再比,必须整个相等;区分大小写 |
| `${customer_code}` | 拦 | 同一个意思的另一种写法 |
| 声明了 `price`,MCP 自由文本里本来就要写 `$price` | **会拦(误伤)** | 只有租户变量名恰好出现在 `$` 之后才会发生,概率很低;提示让模型写实际内容,模型改写即可。不为它开后门 |
| exec_python / bash 代码里的 `$EXPERT_WORK_INPUTS_DIR`、`$org_logo` | 不拦 | 代码工具整个不看 |
| `write_file` 写的 `run.sh` 里有 `$EXPERT_WORK_INPUTS_DIR`、同名 shell 变量 | 不拦 | 文件内容参数排除 |
| 被平台绑定填进去的值里含 `$customer_code` 字样 | 不拦 | 绑定参数排除,那是租户数据 |
| `ask_image(path="$EXPERT_WORK_INPUTS_DIR/x.png")` | 不拦,并且读得到 | 路径参数开头的别名由解析函数认 |
| `read_file(path="$EXPERT_WORK_SKILLS_DIR/a/SKILL.md")` | 拦,提示用 skill_view | 今天这条本来就读不到(解析函数不展开),现在提示更准 |
| 委派任务文字、`update_plan` 里提到 `$EXPERT_WORK_INPUTS_DIR` | **会拦** | 子代的非代码工具同样不展开;改写成 `inputs/current/` 后子代所有工具都认。代价是偶尔多一轮。是否豁免是需要你定的点(§十二 第 3 条) |
| Jinja 写法 `{{ attachment_urls }}` | 不拦 | 不在本次范围(实测没出现) |
| `http` 工具 header 里的 `Bearer $TOKEN` | 不拦 | 不是声明的名字,也不是 `EXPERT_WORK_` |

开销:每次非代码工具调用扫一遍字符串叶子,一个正则找 `$` 标识符再查集合,线性,不需要预算。

---

## 七、兼容和迁移

- **两个环境变量照旧注入、值不变**(`/workspace/inputs/<run_id>/inputs.json` 和它的目录)。已有模板、技能、第三方代码里写 `os.environ["EXPERT_WORK_INPUTS"]`、`$EXPERT_WORK_INPUTS_DIR/x` 的,在代码工具里继续可用。
- **清单 `local_path` 不变**,仍是 `inputs/<run_id>/…`(相对 `/workspace`)。它是代码读的,不经模型手抄,换成短路径没有收益,还会让对外文档的例子失效。
- **旧会话里的旧段**:会话历史里留着写 `$EXPERT_WORK_INPUTS_DIR/…` 的旧段。代码工具里照旧可用;路径参数里由别名接住;压缩后只放回最新一段(新格式)。
- **租户模板**:仓库内 `manifests/`、`configs/`、`platform-skills/` 没有命中。线上租户模板里如果写了,见场景 l。
- **对外文档**(`chat.md:558`、`:657`、`:685-689`):加 `inputs/current/` 的说法,说明它和 `$EXPERT_WORK_INPUTS_DIR` 是同一目录;提示词里地址被换成的本地路径改成新写法。
- **提示词缓存**:有地址变量的 jinja 智能体,系统提示词的字节在发布那一刻变一次(路径和尾注),之后每轮照旧稳定。
- **NAS 上多出的 `inputs/current/` 空目录**:挂载桩,回收任务不碰(不是 UUID 名),控制台不列(保留前缀)。回滚后它就是个空目录,无害。
- **沙箱镜像**:测试、生产(ACS)不用重建;本地后端要重建本地沙箱镜像才有挂载,没重建时 `$EXPERT_WORK_INPUTS_DIR` 照旧可用。
- **没有数据库变更,不用回填。**
- **回滚**:回退代码即可;新旧两种写法在回滚后的旧代码里,`$EXPERT_WORK_INPUTS_DIR` 仍有效,`inputs/current/` 失效(只影响回滚窗口里的会话)。

---

## 八、为什么别名只接 `$EXPERT_WORK_INPUTS_DIR/`,而不是一律拦

用户要求守卫拦 `$EXPERT_WORK_` 开头的字面量。路径参数开头的 `$EXPERT_WORK_INPUTS_DIR/` 例外,因为它有两个正当来源:会话历史里的旧段(发布前每一轮都这么写)、租户自己写的模板。拦下要多一轮,而它的意思毫无歧义(就是本轮输入目录)。别名只认这一个、只认开头;路径中间出现、或是 `$EXPERT_WORK_INPUTS`(清单文件)、`$EXPERT_WORK_SKILLS_DIR`,照拦。是否接受这个例外是需要你定的点(§十二 第 4 条)。

---

## 九、测试计划

每条写明「改坏哪一处会红」。本地 `uv run --no-sync pytest`。先写测试看它红,再实现。

### 9.1 「本轮输入」段(`services/control-plane/tests/test_inputs_block.py`)

| # | 测试 | 改坏哪一处会红 |
|---|---|---|
| B1 | 任何输入组合下段里都没有 `exec_python`、`bash`、`从目录或清单读`、`不要从上文手抄`、`$EXPERT_WORK` | 把 `:115-116` 两句加回去;任一状态文案还带 `$EXPERT_WORK_INPUTS_DIR` |
| B2 | 只有文本变量时没有文件句;有一个地址变量时恰好一句文件句 | 文件句写成无条件;或有文件时漏掉 |
| B3 | trusted 地址 → `文件 inputs/current/org_logo.png`,行里不含 UUID 形状的串 | 路径前缀没改;误把 run id 拼进路径 |
| B4 | untrusted 地址 → 只有 `inputs/current/` 与「名字以 {var} 开头」,不含扩展名、不含链接名 | 把真实链接名写进 untrusted 行(P16) |
| B5 | 列表 / 对象:项数、文件数对;untrusted 时说明 slug、字典键不出现 | 计数错;占位符被真名替换 |
| B6 | 被绑定且传了 → 状态后接 `；由平台填进绑定它的工具参数`;被绑定没传 → 只有 `本轮未提供` | `_status` 里判断顺序被调换(P12) |
| B7 | 零值语料(带签名 query 的 URL、长 token、中文说明、JSON 字符串值):段里不含任何值的任何 8 字以上子串 | 任何一处把值或由值推出的名字写进段 |
| B8 | untrusted 文本 → `已提供（外部数据）`,不含 `清单` | `_UNTRUSTED_TEXT` 回退 |
| B9 | 没有声明变量 → `None` | 早返回被删 |
| B10 | `block_stats` 三个数在上述语料上与现值相同 | 计数语义被顺手改了 |
| B11 | `inputs_block_message` 仍带 `HIDE_FROM_UI` 与 `INPUTS_BLOCK_MARK`;现有 `test_runs_inputs_block.py`、`test_spawn_run_supersede.py`、`test_supersede_kernel_integration.py`、压缩放回的用例全绿 | 改坏打标 → regenerate 不带走、压缩后不放回 |

### 9.2 提示词渲染(`services/control-plane/tests/test_prompt_render.py`)

| # | 测试 | 改坏哪一处会红 |
|---|---|---|
| R1 | 字符串地址(trusted / untrusted)渲染出 `inputs/current/{link}`,全文不含 `EXPERT_WORK` | `_render_url`(`:181`)没改 |
| R2 | JSON 字符串列表 / untrusted 列表的逐项块用短路径 | `_render_items`(`:118`)没改 |
| R3 | trusted 真 list:`{% for m in materials %}{{ m.url }}{% endfor %}` 每项是短路径;`{{ materials | length }}` 不变 | `render_value` 结构副本(`:217`)没改 |
| R4 | 尾注逐字等于新 `URL_NOTE`,不含「下载」二字 | 尾注回退 |
| R5 | 非 jinja 智能体输出逐字等于存储的提示词 | 改动漏到非 jinja 路径 |

### 9.3 短路径解析(`services/orchestrator/tests/test_workspace_paths.py` 等)

| # | 测试 | 改坏哪一处会红 |
|---|---|---|
| P1 | `inputs/current/x.png`、`/workspace/inputs/current/x.png` → `rel == "inputs/<R>/x.png"`,`path` 保留短写法 | 改写一步缺失或放在折叠之前 |
| P2 | `$EXPERT_WORK_INPUTS_DIR/x.png`、`${EXPERT_WORK_INPUTS_DIR}/x.png` → 同 P1;`a/$EXPERT_WORK_INPUTS_DIR/x` 不改写 | 别名缺失;别名不限开头 |
| P3 | `inputs_run is None` 时 `inputs/current/x` 报「本轮没有输入变量」 | 静默放行成 `inputs/current/x`(宿主侧读到空桩、报「不存在」) |
| P4 | `inputs/<uuid>/x`、`inputs/cache/x`、`inputs/currentx/y`、`/agent/inputs/current/x`(老视图)都与今天结果相同 | 改写范围过宽 |
| P5 | 对话视图:`agent_relative` 得到 `threads/<conv>/work/inputs/<R>/x` | 对话视图下拼错前缀 |
| P6 | `ToolContext.workspace_view.inputs_run`:主 run = `run_id`;有 `inputs_run_id` 用它;`prompt_inputs` 为空时 `None`;`_child_config` 出来的子代 = 父首段 | 子代用了自己的 `sub_run_id`;没 inputs 也给了值 |
| P7 | `read_page` 对两轮同名 `inputs/current/report.pdf` 算出不同 `doc_sha` | `rel` 没换成真实路径 |
| P8 | `run_in_sandbox` 传给 `client.exec` 的环境变量 run id 与挂载 run id 是同一个值(都来自 `view.inputs_run`) | 两处各算各的、走散 |

### 9.4 沙箱挂载(`test_exec_view.py`、`test_exec_view_kernel.py`、`test_runner_unit.py`)

| # | 测试 | 改坏哪一处会红 |
|---|---|---|
| X1 | `exec_view_argv(..., inputs_run_id=R)` 的 `$7` 是 `str(R)`;没绑智能体或 `None` 时是空串 | argv 漏传或位置错 |
| X2 | 脚本对 `$7` 不是 UUID 形状(含 `/`、`..`、大写)以 98 退出 | shell 校验缺失 |
| X3 | 真命名空间:对话视图、老视图各一例,exec 里 `open("inputs/current/org_logo.png")` 读到缓存里的字节;`inputs/current/materials/0-x.mp4`(两层链接)也读得到 | 挂载缺失;挂到了 `/workspace/inputs` 上把 `../cache` 弄断 |
| X4 | 同一工作区两个 run 并发 exec,各自的 `inputs/current` 是各自的 run | 用了平铺链接或共享符号链接 |
| X5 | `inputs/current` 被预先换成符号链接 / 普通文件,或 run 目录不在:exec 照常成功、stderr 有一行、`inputs/current` 没挂 | 失败改成了退出;或跟着符号链接挂到了别处 |
| X6 | 载荷里 `umount inputs/current` 失败 | 挂载放到了载荷所在的 user namespace |
| X7 | 现有 `test_exec_view_script_matches_the_sandbox_image` 照旧(两份脚本相等)、`freeze` 用例照旧 | 只改了一份脚本;新挂载没记进 `OURS` 被冻成只读 |
| X8 | 本地后端请求体不带 `inputs_run_id` 时 argv `$7` 为空串 | 新镜像对老请求体崩 |

### 9.5 宿主侧读链接(`test_nas_workspace_store.py`、`test_vision*.py`)

| # | 测试 | 改坏哪一处会红 |
|---|---|---|
| L1 | **先跑一次证实现状**:今天 `ask_image(path="inputs/<R>/x.png")`(x 是预下载链接)报越界拒绝 | —(证实 §1.2 第 3 条;若它其实是绿的,本节方案收缩为不做) |
| L2 | `resolve_input_link` 把 `inputs/<R>/x.png -> ../cache/<32hex>.png` 解析成 `inputs/cache/<32hex>.png`,`ask_image(path="inputs/current/x.png")` 成功 | 链接解析没接进 `ask_image` |
| L3 | 指向 `../../../etc/passwd`、`../cache/sub/x`、`../other/x`、绝对路径的链接 → 原样返回,随后照旧被拒 | 目标校验放宽 |
| L4 | `inputs/<R>/` 之外的符号链接不解析 | 范围放宽到整个工作区 |

### 9.6 守卫(`services/orchestrator/tests/test_input_literal_guard.py` + 接线测试)

| # | 测试 | 改坏哪一处会红 |
|---|---|---|
| G1 | MCP 调用非绑定参数里有 `$attachment_urls` → 不派发,消息含绑定参数名 | 守卫没接进 `tools_node`;命中后仍派发 |
| G2 | §六表里「不拦」的每一行各一例 | 标识符没贪婪取完(`$customer` 误中);数字 / 空格当成名字;大小写不分 |
| G3 | `${customer_code}` 拦 | 只认 `$name` 一种写法 |
| G4 | exec_python / bash 里同样的文本不拦 | 代码工具没排除 |
| G5 | `write_file` 内容参数不拦;同一调用的 `path` 参数里有 `$org_logo` 拦 | 排除范围写成整个工具 |
| G6 | 绑定参数里填进去的值含 `$customer_code` 不拦 | 扫描发生在填值之后却没排除绑定参数 |
| G7 | 路径参数开头 `$EXPERT_WORK_INPUTS_DIR/` 不拦;中间出现拦 | 别名例外过宽 / 缺失 |
| G8 | `$EXPERT_WORK_SKILLS_DIR` 的提示含 `skill_view`;MCP 的提示不含 `inputs/current` | 提示分支错 |
| G9 | 子代(只有 `prompt_inputs`,图里无声明)里写 `$org_logo` 也拦 | 名字集合只取了本图声明 |
| G10 | 审计 `reason=input_variable_literal`,details 只有变量名与参数名,不含任何参数值 | 把 args 原样记进审计 |
| G11 | 判定函数抛异常 → 整批放行,只记异常类型 | 异常冒出去让 run 失败 |
| G12 | 审批续跑路径重算出同样的命中 | 命中依赖了非确定输入 |

### 9.7 工具描述(`test_exec_python_tool.py`、`test_bash_tool.py`)

| # | 测试 | 改坏哪一处会红 |
|---|---|---|
| D1 | 两个描述都包含同一个 `INPUTS_CODE_NOTE` | 一边改了另一边没改 |
| D2 | 常量里有 `$EXPERT_WORK_INPUTS`、`不要从上文手抄`、`inputs/current/`、`local_path`、`原地址下载` | 保护被删、路径说法没改 |
| D3 | 手抄 URL 守卫的两条提示含 `inputs/current/`、不含 `$EXPERT_WORK_INPUTS_DIR` | `input_url_guard.guard_message` 没改 |

### 9.8 真栈验收(测试环境,改完再跑)

1. 健管师按 B-314 测评脚本重跑同一组对话(或至少前 50 轮):`exec_python` 为读输入值的调用 ≤ 2 次(原 19 次);`$变量名` 字面量到达 MCP 的次数 = 0;守卫命中次数如实记下。
2. 一个文档分析智能体(trusted PDF 地址 + 素材列表):`read_document("inputs/current/report.pdf")`、`ask_image(path="inputs/current/materials/0-…jpg")`、`exec_python` 打开同一个文件,三条都成功。
3. 一个委派场景:子代用 `inputs/current/` 读到父那一轮的文件。
4. 同一轮里审批挂起后续跑:续跑段的 `inputs/current/` 仍是首段的文件。

跑之前按评测要算钱的规矩先估费用、只跑受影响的用例。

---

## 十、工作量

| 项 | 人天 |
|---|---|
| 第 1 点:段文案、事实句、绑定说法 + 测试 B1-B11 | 0.5 |
| 第 2 点宿主侧:`WorkspaceView.inputs_run`、解析改写、别名、`run_in_sandbox` 统一取值 + 测试 P1-P8 | 0.5 |
| 第 2 点沙箱侧:exec 脚本 `$7` 与挂载(两份)、两个后端传参 + 测试 X1-X8(含真命名空间用例) | 1 |
| 第 2 点链接解析:`resolve_input_link` + `ask_image` 接入 + 测试 L1-L4 | 0.5 |
| 第 2 点提示词渲染 + 测试 R1-R5 | 0.25 |
| 第 3 点守卫:纯函数、接线、审计 + 测试 G1-G12 | 1 |
| 第 4 点描述常量、URL 守卫提示 + 测试 D1-D3;对外文档、注释 | 0.25 |
| 审查往返 | 0.5 |
| 真栈验收 | 0.5 |
| **合计** | **约 5 人天** |

---

## 十一、风险

1. **exec 视图脚本是安全关键代码**(经过多轮安全审查)。新挂载必须照现有规矩按 fd 打开、`O_NOFOLLOW`、失败只跳过不跟随;单独请安全审查看这一段。
2. **本地后端脚本在镜像里。** 本地沙箱镜像不重建时,老脚本不认 `$7`:本地后端的 argv 由镜像里的 `runner.py` 自己拼,新 orchestrator 只是多传一个请求字段,老镜像忽略它,行为与今天相同(没有挂载,环境变量照旧)—— 不会崩,但本地开发看不到短路径。ACS 的脚本和 argv 都由 orchestrator 一起发,不存在新旧错配。
3. **`WorkspaceView` 多一个字段**,它的相等 / 哈希如果被当成缓存键或锁键,会多出不该有的区分。实现时先 grep 所有用到 `WorkspaceView` 相等或哈希的地方;有的话把 `inputs_run` 设成 `compare=False` 或改从 `ToolContext` 另取。
4. **「current」永远指本轮。** 历史里上一轮的段说 `inputs/current/x`,这一轮 `inputs/current/` 已经是新的。这和今天 `$EXPERT_WORK_INPUTS_DIR` 完全一样,不是新问题;压缩后只放回最新一段。
5. **模型仍可能出于好奇去读值**(场景 g)。命令删了,`is_first_contact：已提供` 本身不再引导,但模型可能想知道值。真栈验收第 1 条量化;若仍超标,再考虑对非文件变量写「值不在本段」之类的事实,不加命令。
6. **守卫误伤**:见 §六,两类已知(租户变量名恰好出现在 `$` 后、委派 / 计划文字里提到环境变量)。代价都是多一轮。
7. **守卫在审批门之后**:一个需要审批的 MCP 调用里写了 `$变量名`,人先被问到,批准后才被拦。沿用 URL 守卫的口径(不改审批门的判定与下标);若要提前到审批门之前,是另一个决定(§十二 第 6 条)。
8. **`ask_image` 链接解析多一次 NAS 读**(`readlinkat`),只在 `inputs/<UUID>/` 下发生,可以忽略。

---

## 十二、需要你定的点

1. **短路径的名字**:推荐 `inputs/current/`。不选扁平 `inputs/<文件>` 的理由见 §3.2.1(并发串、链接断、`local_path` 契约断)。备选 `inputs/this_turn/`、`inputs/turn/`。
2. **被绑定变量怎么写**:推荐只写「由平台填进绑定它的工具参数」、不列工具名(§3.1 第 4 条)。备选:列出工具名和参数名(每轮多一二百 token,随会话累积)。
3. **守卫的豁免范围**:推荐只豁免代码工具、写文件工具的内容参数、绑定参数、路径参数开头的别名;委派任务文字和 `update_plan` 照拦(§六)。备选:再豁免委派与计划类工具。
4. **路径参数里接受 `$EXPERT_WORK_INPUTS_DIR/` 别名**:推荐接受(§八)。备选:一律拦。
5. **系统提示词的尾注改成事实**:推荐改(§4.2),代价是有地址变量的智能体在发布时提示词缓存失效一次。备选:只改路径、尾注不动。
6. **守卫放在审批门之前还是之后**:推荐沿用 URL 守卫,放在之后(§十一 风险 7)。
