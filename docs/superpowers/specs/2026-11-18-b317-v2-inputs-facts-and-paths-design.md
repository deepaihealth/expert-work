# B-317 v2 「本轮输入」只陈述事实 + 所有工具都认的短路径 设计

> 状态:第 2 版,待复审。2026-11-18。
> 范围:四件事一起做 —— ①「本轮输入」段只写事实 ② 一个所有工具都认的短路径 ③ 非代码工具参数里的 `$变量名` 守卫 ④ 两个代码工具的描述。
> 不复用旧分支 `fix/b317-inputs-block-no-needless-file-read` 的分类思路(按 Jinja 语法树判断变量进没进提示词,被审查否掉:误判一大片)。本方案不判断「变量在不在提示词里」,只写平台确定知道的事实。

**第 2 版改了什么**(用户 11-18 定了第 1 版 §十二 的 6 点,独立审查的结论一并处理):

| 来源 | 改动 | 位置 |
|---|---|---|
| 用户定 1、2、5 | `inputs/current/`;绑定只写「由平台填进绑定它的工具参数」;系统提示词尾注改成事实 —— 都按推荐,正文不变 | §3.1、§3.2、§4.2 |
| 用户定 3 | 守卫改成**白名单受检面**:只查 MCP / http 的全部字符串叶子(绑定参数除外)、平台工具的 `path_args`、`ask_image.path` | §3.3 重写、§六 重写 |
| 用户定 4 + 审查 H1 | 别名照收;`ResolvedPath.path` 与一切落盘 / 持久化 / 元数据都写**真实路径** `inputs/<run>/…`,短路径只出现在给模型看的文本里 | §3.2.2 |
| 用户定 6 | 守卫仍在审批门之后;加计数「已批准后被字面量守卫拦下」 | §3.3.2 |
| 审查 L1 | 协调方已实测证实,`resolve_input_link` 必做;探针作为正式测试提交进仓库 | §1.2、§9.5 |
| 复审 LOW ×9 | 环境变量注入口径、挂载异常与 run id 双重校验、保留期措辞、场景 o / p、受检面 ② 用自维护的表、审计措辞、补测试、M3 已知漏拦 | §3.2、§3.3、§五、§六、§七、§九 |
| 实现复审 M1 | 用短路径(或别名)调用路径类工具成功时,平台 notice(围栏外)补一句「（之后的轮次请用 inputs/<run>/…）」;「本轮没有输入」的报错改成指向这句与 `list_dir` 看 `inputs/`,不再说渲染页段里有路径 | `builder._with_later_turns_path`、`workspace_paths._current_inputs_parts` |
| 实现复审 L1 | 守卫规则 3 只在本轮有输入时查,只认 `inputs/current/` 与本轮 run 的 `inputs/<run>/`,不认 `inputs/cache/` 与别的 run;规则 2 照旧对所有智能体生效(前缀归平台,外部服务与路径参数里没有合法的字面用途) | `input_literal_guard.find_literal` |
| 实现复审 L2 / L3 | 别名豁免先去首尾空白;`list_dir` 只在 `inputs/<run>/` 底下读链接、复用已开的目录 fd;`ask_image` 只对 `inputs/` 路径问存储 | — |
| 实现复审 存疑 | 守卫拦下的调用不进 B-302 文件账。核实:账本只对 `edit_file` 的 `COUNTED_EDIT_FAILURES` 计数、只记成功的改动,被拦调用无 `failure_kind`,原先也不影响阈值;改了是为口径一致 | `builder.py` 文件账 |
| 安全复审 S1 | `current` 被换成符号链接 / 普通文件时 `unlink`(不跟随)后重建再挂;删不掉照旧静默跳过 | `exec_view.current_inputs`(两份字面量) |
| 第三轮复审 L1 + 存疑 | 写 `inputs/current/<x>` 或 `inputs/<run>/<x>` 的调用失败且这个文件不在时,宿主侧列出别的轮次里同名的 `inputs/*/<x>`(新的在前、最多 5 个)接在报错后面:不靠回执里那句(可能被上下文裁剪删掉),也不让模型手抄 36 位 run id。本轮有输入、但文件属于前一轮的,同样列 | `tools/inputs_candidates.py`、`builder._inputs_candidates_hint` |
| 第三轮复审 L2 | 规则 3 认任一轮的 `inputs/<规范 UUID>/`(之后的轮次模型照回执用前一轮的);本轮有输入**或智能体声明了变量**时查,两者都没有照旧不查 | `input_literal_guard._LOCAL_INPUT_RE` |
| 第三轮复审 L3 | 外部服务参数里的 `$EXPERT_WORK_*` 只在整段像路径时拦(以它开头,或整段没有空白);脚本、SKILL.md 正文里提到它放行。路径参数不变 | `input_literal_guard._path_shaped` |
| 第三轮复审 L4–L6 | 补「工具报告失败不补那句」的测试;「之后的轮次」那句只回显链接名形状的路径(`[\w./-]`,≤240 字);`ask_image.image_ref` 填工作区路径时补那句、守卫也查 | `builder._with_later_turns_path`、`PATH_ARGS` |
| 第三轮安全复审 | 内核测试补 `current` 被种成 FIFO / 硬链接;符号链接那条改成指向工作区里有内容的目录并断言内容不变 | `test_exec_view_kernel.py` X5 |
| 第四轮复审 LOW-1 / LOW-2 | 列候选不再借列目录判断在不在(列 run 目录会对每条链接读链接、看缓存文件):存储加只做 `O_NOFOLLOW` 逐段打开 + `lstat` 的 `path_exists`,只列一次 `inputs/`;总时限 2 秒,超时不列;某个 run 查不了只跳过那一个 | `NasWorkspaceStore.path_exists`、`inputs_candidates.input_candidates`、`builder._CANDIDATES_TIMEOUT_S` |
| 第四轮复审 LOW-4 / LOW-5 | 外部服务长正文的放宽只给 `$EXPERT_WORK_SKILLS_DIR`,别的 `$EXPERT_WORK_*` 照拦;规则 3 补 `./inputs/…` 与 `/agent/inputs/<规范 UUID>/…` | `input_literal_guard` |
| 第四轮复审 LOW-3 / LOW-7 | `inputs/` 列表按名字截断后才按时间排、240 字上限:只在注释里写明取舍,不改 | `inputs_candidates._run_dirs`、`builder._LINK_SHAPED_PATH_RE` |
| 审查 M1 | 不加 `$7`,exec 视图脚本从已下发的环境变量 `EXPERT_WORK_INPUTS_DIR` 取 run id | §3.2.3 重写 |
| 审查 M2 | run 目录不在时挂载阶段静默跳过,不写 stderr;run 目录在才建 `current` 桩 | §3.2.3 |
| 审查 M3 | MCP / http 的字符串叶子整段是 `inputs/…` 本地路径时也拦 | §3.3.1 |
| 审查 LOW ×8 | 每条一句处理 | §3.5 |
| 审查 | 跨场景表补 4 行;工作量重估 | §五、§十 |

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
3. **宿主侧读不了预下载的链接(协调方实测证实)。** 预下载建的是**符号链接** `org_logo.png -> ../cache/<摘要>.png`(`tools/prefetch_script.py:160-185`),而宿主侧读工作区文件的两条路 —— `NasWorkspaceStore.read_file`(`tools/nas_workspace_store.py:1063-1095`)和 `NasWorkspaceImageResolver`(`multimodal.py:188`,末段读在 `_read_workspace_leaf`,`:243`)—— 末段都用 `O_NOFOLLOW`。协调方的探针(本地临时目录下建好上面那个链接,调 `NasWorkspaceImageResolver.resolve`)得到 `ValueError: workspace image ref escapes the user root`。所以今天就算模型手写出真实路径 `inputs/<run_id>/org_logo.png` 交给 `ask_image`,也读不到。`read_document` / `read_page` 在沙箱里按 `realpath` 读,链接在 `/workspace` 之内,这两个读得到。

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
4. 被绑定的变量:写「由平台填进绑定它的工具参数」,**不列工具名**(用户已定)。理由:业界做法是被注入的参数模型根本看不见(LangChain `InjectedToolArg`);我们还说一句,只是因为变量名本身出现在这一段里,模型需要知道「这个值不归你传」。列工具名每轮多一二百 token、随会话累积,却不改变模型能做的事。真写错时,守卫(第 3 点)的提示会点名「本工具的哪个参数由平台填」。
5. 「本轮未提供」优先(裁定 P12 不变):可选变量被绑定但本轮没传,只写「本轮未提供」。
6. **零值原则不变**:段里不写任何值;untrusted 变量的真实文件名(扩展名来自租户 URL)不写,列表项说明、字典键用占位符。
7. **按能力门控**(审查 LOW,照「平台提示词跟着能力走」):文件句和每行里的文件路径,只在这个智能体有至少一个能读文件的工具时才写 —— `exec_python`、`bash`、`read_document`、`read_page`、`ask_image` 任一个在它的工具表里(`BuiltAgent.tool_catalog`,`orchestrator/built_agent.py:95-98`)。都没有时,文件变量的状态写成「已提供」(被绑定的照接绑定那句),不出现文件句。现实里 `exec_python` 对每个智能体都装(B-314 01 §可选二 11-18 更正),所以眼下这道门恒开;它是结构上的保证,以后代码执行能关时不用再回来改。`build_inputs_block` 加一个参数 `reads_files: bool`,由 `api/runs.py:596-610` 按 `built.tool_catalog` 算好传进来。

**不变的东西**(实现时不碰,测试钉住):

- 消息构造 `inputs_block_message`、打标 `INPUTS_BLOCK_MARK`(`inputs_block.py:138-148`)不变 → regenerate 重放(`supersede.py:410-419` 按标认段)照旧带走它;压缩后 `_keep_latest_inputs_block`(`graph_builder/builder.py:2799`)照旧放回最新一段。
- `block_stats` 三个计数(`inputs_block.py:125-135`)语义不变:`variable_count` / `bound_count` / `url_count`。
- 生成时机和位置不变(`api/runs.py:596-610`,只在 jinja 智能体上、贴在用户消息之后)。
- 说明文字压一行、限 40 字不变。

### 3.2 第 2 点:所有工具都认的短路径 `inputs/current/`

#### 3.2.1 为什么是 `inputs/current/`(用户已定)

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

1. **并发会串。** 同一个 `/workspace` 可能被多个 run 同时用:老视图(B-236 之前开的对话)里是整个智能体根,同一用户的多个对话共用;对话视图里,审批挂起的旧一轮续跑时,新一轮可能已经写过。链接平铺在 `inputs/` 下,后写的覆盖先写的。
2. **符号链接会断。** 在沙箱里把本轮目录「盖」在 `/workspace/inputs` 上,链接 `../cache/x` 就会指到 `/workspace/cache/x`,不存在。
3. **`local_path` 契约会断。** 清单里 `local_path` 是 `inputs/<run_id>/org_logo.png`(相对 `/workspace`),对外文档(`apps/admin-ui/docs-site/guide/chat.md:558`、`:685-689`)教第三方这么用。

`inputs/current/` 和 `<run_id>/` 在同一层,三个坎都没有:每次 exec 在自己的挂载命名空间里把本轮的 `inputs/<run_id>/` 挂到 `inputs/current/`,并发的两个 exec 各看各的;挂载点与 `<run_id>/` 同深度,`../cache/x`、`../../cache/x` 照常解析;已有路径和两个环境变量的值一个字不改。

**名字 `current` 不会撞**:run 目录名是 UUID,缓存目录叫 `cache`;回收任务只收 UUID 名的目录(`control_plane/workspace_janitor.py:220`、`:231`);变量叫 `current` 也没事,它的链接在 `inputs/<run_id>/` 里面。`inputs/` 是保留前缀(`expert_work/persistence/workspace/layout.py:266-268`),控制台文件浏览和工作区快照都不列它。对话视图里 `inputs/` 是本对话 `threads/<对话>/work/` 下的普通子目录,老视图里在智能体根下;`inputs/current/` 跟着 `inputs/` 走,不碰 `threads/`、`uploads/`、`.tool_results/`。

**「current」永远是「这一轮的输入」**,和今天 `$EXPERT_WORK_INPUTS_DIR` 的语义完全一样。上一轮的文件仍在 `inputs/<上一轮 run_id>/`。所以凡是要**跨轮**记住、再用的路径,都必须是真实路径(§3.2.2 第 4 条)。

#### 3.2.2 平台工具:一个解析函数

**改 `orchestrator/tools/workspace_paths.py`**:

1. `WorkspaceView`(`:172-199`)加字段 `inputs_run: UUID | None = None` —— 本轮输入目录属于哪个 run。
2. `ToolContext.workspace_view`(`tools/registry.py:456-463`)里填它:`(inputs_run_id or run_id) if (prompt_inputs and agent_key) else None`。前半是 `run_in_sandbox` 今天给环境变量用的表达式(`tools/sandbox.py:962`);加上 `agent_key` 是为了与沙箱侧口径一致(审查 LOW,见 §3.5 第 6 条)。`run_in_sandbox` 改成从 `view.inputs_run` 取环境变量用的 run id,于是环境变量、沙箱挂载(§3.2.3 从环境变量取)、宿主侧解析**结构上是同一个值**。
3. `resolve_workspace_path`(`:282-358`)在折叠 `/workspace/` 之后、拆段之前加一步:
   - 头两段是 `inputs` + `current` → 改写成 `inputs/<inputs_run>/<余下>`。
   - **兼容别名**(用户已定):开头是 `$EXPERT_WORK_INPUTS_DIR/` 或 `${EXPERT_WORK_INPUTS_DIR}/` 时,当成 `inputs/current/` 处理(只认开头)。
   - `inputs_run` 为 `None` → 报错(文案见 §4.5)。
   - `/agent/inputs/current/...`:老视图里不改写、直接报错并指路(文案见 §4.5;审查 LOW)—— 沙箱只在 `/workspace` 一侧挂载,老视图里 `/agent/inputs/current` 是空桩,宿主侧和沙箱侧要说同一句话。对话视图里 `/agent/inputs/` 本来就拒(`:344-351`),不变。
   - `inputs/<run_id>/...`、`inputs/cache/...`、`inputs/currentx/...` 都不改写,与今天相同。
4. **真实路径与显示路径分开(审查 H1)**。`ResolvedPath`(`:202-214`)今天的 `path` 字段既被拿去给模型看,也被落盘。改成:
   - `path` = **改写后的真实路径** `inputs/<run>/…`(别名同样)。凡是持久化、跨轮、写元数据的地方继续用 `path`,于是一律是真实路径:`read_page` 的 `figure_documents`(`tools/read_page.py:1553` 取、`:1751`、`:1814` 写进 state)、`ask_image` 的结果元数据(`vision.py:1482`、`:1493`、`:1496`、`:1508`)、渲染页 ref。审计不在本次范围:它记的是模型传进来的原始参数(`builder.py:4317-4330`),照旧。
   - 新字段 `display` = 模型这一次能直接再用的写法:短路径或别名输入时是 `inputs/current/…`(别名也归一成短路径),其余等于 `path`。只用在**这一次工具调用**回给模型的文本里(回执、报错)。
   - 为什么:`figure_documents` 是跨轮的。下一轮 `figure_block._document_path`(`graph_builder/figure_block.py:193-205`)用**下一轮的** view 把记下的路径再算一遍 `doc_sha` 去核对。记的若是 `inputs/current/report.pdf`,下一轮它指向新一轮(或报「本轮没有输入」),哈希对不上 → 渲染页段丢了路径;更糟的是新一轮恰好也有一份 `report.pdf`,就指到另一份文档。记真实路径,哪一轮核对都是同一个文件。
   - 实现时 grep `ResolvedPath` 的 `.path` 全部使用点,按「落盘 / 跨轮」与「本次回给模型」分两类,前者不动(自动变成真实路径),后者改用 `.display`;清单写进实现计划。
5. `read_page` 的 `doc_sha` 本来就按 `rel`(真实路径)算(`read_page.py:1313-1317`),两轮同名报告各算各的,不受影响。

走这个函数的工具(都已经在用,不用逐个接):文件工具(`tools/file_ops.py:170`)、`read_document`(经 `_require_path`)、`read_page`(`read_page.py:1313`)、`ask_image`(`vision.py:1506`)、`save_artifact`(`artifact.py:142`)。

**宿主侧读链接(L1,必做)**:在 `NasWorkspaceStore` 加一个窄函数 `resolve_input_link(tenant_id, user_id, path) -> str`:

- 只处理父目录是 `…/inputs/<UUID>/…` 的末段;
- 在已钉住的父目录 fd 上 `readlinkat`(不跟随),目标按链接所在目录归一化后必须**恰好**是同一个 `inputs/` 下的 `cache/<一段文件名>`,且文件名以 32 位十六进制开头(预下载缓存的命名,`prefetch_script.py:261-268` 的 `cache_digest`);
- 满足就返回缓存文件的路径,不满足(普通文件、别的链接、目标越界)原样返回,后面照旧按 `O_NOFOLLOW` 读 —— 越界链接仍然被拒。

`ask_image._workspace_image`(`vision.py:1498-1508`)在拼引用前调它一次。不放宽安全边界:能被认的只有指回本工作区缓存目录的链接,沙箱代码本来就能读那个文件。`read_file` 不接:预下载只收图片、音视频、PDF、Office(`prefetch_script.py:246` 的 `content_type_ok`),`read_file` 只读文本。非 NAS 的存储实现默认原样返回。

#### 3.2.3 沙箱里的 exec_python / bash:每次 exec 挂一次(审查 M1、M2)

**不加位置参数**。挂载用的 run id 从已经下发给 exec 的环境变量 `EXPERT_WORK_INPUTS_DIR` 取。

为什么这样更好:

- 本地链路有一跳上一版漏了:`HTTPSupervisorRuntime.exec` → `ExecRequest`(sandbox-supervisor `schemas.py:88-118`,没有 `extra=forbid`,多余字段被静默丢掉)→ app → supervisor → `runner_link` → `runner.py`。加 `$7` 要改 argv、schema、runner_link、runner 四处,任何一处漏掉都是静默失效。
- 环境变量两条后端都已经带到脚本面前:ACS 是 `commands.run(..., envs=envs)`(`tools/agent_sandbox.py:1681-1706`),本地是 `subprocess.run(argv, env=child_env)`(`infra/sandbox-image/runner.py:594-603`),argv 的第一项就是 `unshare` → `sh -c` 脚本,脚本和内嵌 python 都看得到。
- 挂载与环境变量由同一个值派生,不可能一个指 A 轮、一个指 B 轮。
- 这个变量由平台注入(`sandbox.py:142-145`),载荷在脚本之前没有机会改它:外层 `bash -l` 的 `HOME` 已换成 `/`、`BASH_ENV` / `ENV` 置空(`exec_view.py` 的 `EXEC_SHELL_ENV`),读不到载荷写得进的 profile。

我没有找到不能接受的问题。代价只有一条:挂载阶段要多认一个环境变量的格式,这个格式由 `inputs_doc.inputs_abs_dir`(`inputs_doc.py:91-93`)决定;加一条契约测试钉住两边(§9.4 X1)。

**改 `orchestrator/tools/exec_view.py` 的 `EXEC_VIEW_SCRIPT` 和它在镜像里的逐字副本 `infra/sandbox-image/runner.py:76-`**(契约测试 `test_exec_view_script_matches_the_sandbox_image` 钉两份相等;argv、`exec_view_argv`、`build_exec_command`、`ExecRequest`、`runner_link` 都不动):

1. shell 部分取 `inp="${EXPERT_WORK_INPUTS_DIR-}"`,用 `case` 校验它**恰好**是 `/workspace/inputs/<36 位小写 UUID 形状>`。UUID 那一段逐位写成明确的字符类(`[0-9a-f]` 八位、`-`、四位……共 36 位),**不用 `*`** —— `*` 在 `case` 里能匹配 `/`,`/workspace/inputs/x/../y` 这类就混进来了:是就取最后一段作 run id,否则(没设、空、形状不对)run id 为空串。run id 作为脚本内部的一个参数交给内嵌 python(脚本对外的 `$1`~`$6` 不变)。形状不对不报错、不退出:这是一个方便性的挂载,不是安全边界,失败方向是「不挂」,环境变量照旧可用;格式由契约测试保证。
2. 内嵌 python 的 `nas_view`(`exec_view.py:317-366`)在 `/workspace` 绑好之后、`shared/` 之前加一步,全程按 fd(与现有挂载同一套写法):
   ```
   if run_id:
       try:
           inputs = sub(ws, "inputs", False)     # 不建:没有 inputs/ 就没有本轮清单
           run    = sub(inputs, run_id, False)   # O_NOFOLLOW,必须是已存在的真目录
                                                 # (run_id 已过 str(UUID(x)) == x)
       except OSError:
           run = None                            # 静默:不写 stderr
       if run is not None:
           try:
               stub = sub(inputs, "current", True)   # run 目录在才建桩
           except OSError:
               stub = None                           # 被换成符号链接 / 普通文件:静默
           if stub is not None:
               try:
                   bind(run, stub)                   # open_tree + move_mount,记进 OURS
               except OSError:
                   pass                              # 静默:不挂
   ```
   - **静默跳过(M2)**:run 目录不在(清单写入失败只降级;审批挂得太久目录被回收)、`current` 被换成符号链接或普通文件,一律什么都不写。照 `hide_memory` 的做法:stderr 就是工具输出,写一行会被 B-226 的收尾判定当成失败。
   - **只在 run 目录确实存在时才建 `current` 桩**,不会每次 exec 都去建目录。
   - `bind(run, stub)` 本身也包在 `try` 里:`open_tree` / `move_mount` 失败同样静默不挂,不让 exec 失败。
   - **python 侧再校验一次 run id**:`str(UUID(x)) == x`,不等就不挂。shell 的 `case` 与 python 的 UUID 校验各自独立成立,任何一层被改松,另一层仍挡住。
   - 挂载可写,和今天 `inputs/<run_id>/` 可写一样(预下载要往里写),能力不比今天多;记进 `OURS`,`freeze()` 不会把它冻成只读。
   - 载荷跑在嵌套 user namespace 里,卸不掉这个挂载(与现有各层同理)。
   - 只挂 `/workspace` 一侧,不挂 `/agent`。
   - 没绑智能体(`root` 为空,`nas_view` 走早返回分支)时不挂;宿主侧此时 `inputs_run` 也是 `None`(§3.2.2 第 2 条),两侧一致。
3. `run_in_sandbox`(`sandbox.py:962-978`)只改一处:传给 `client.exec` 的 `run_id` 取 `ctx.workspace_view.inputs_run`。

**生效范围**:ACS(测试、生产)的脚本由 orchestrator 每次随命令发过去,随 orchestrator 发布立即生效,不用重建沙箱镜像。本地后端的脚本在镜像里,重建本地沙箱镜像后才有挂载;没重建时一切与今天相同(没有挂载,环境变量照旧)。没有新旧错配的组合。

#### 3.2.4 委派出去的子代、后台任务

不需要新通道。子代的 `inputs_run_id` 和 `prompt_inputs` 已经从父那里带下来(`tools/_child_run.py:1017-1019`、`:1048-1049`,字段说明 `registry.py:396-424`),于是子代的 `view.inputs_run` = 父那一轮首段的 run id;环境变量也是这个值(今天就是),于是子代 exec 挂的、宿主侧解析到的都是父那一轮的目录。对话视图里子代的 `/workspace` 就是父对话的 `work/`,老视图里是同一个智能体根,`inputs/<父 run_id>/` 都在。后台任务和子代即使跨过了父对话的下一轮,也仍挂父**派它那一轮**的目录 —— 这是对的,见 §五 场景 q。

#### 3.2.5 系统提示词里的路径

**改 `control_plane/prompt_render.py`** 三处,`$EXPERT_WORK_INPUTS_DIR/{link}` → `inputs/current/{link}`:`_render_items`(`:118`)、`_render_url`(`:181`)、`render_value` 里 trusted 真 list / dict 的结构副本(`:217`)。尾注 `URL_NOTE`(`:43`)改成事实(用户已定),逐字见 §4.2。`render: raw` 的变量不走这里(`:202-203`),照旧给原值。

#### 3.2.6 还有谁依赖 `$EXPERT_WORK_INPUTS_DIR` / `$EXPERT_WORK_INPUTS`(全仓库 grep)

| 位置 | 用途 | 处理 |
|---|---|---|
| `tools/sandbox.py:106-146` `agent_key_envs` | 往沙箱注入两个环境变量 | **保留,值不变**;现在 exec 视图脚本也读它 |
| `tools/sandbox.py:1107-1121`、`tools/bash.py:98-112` | 代码工具描述 | 改(第 4 点) |
| `control_plane/inputs_block.py`、`prompt_render.py:40-41` | 段、提示词 | 改成短路径;两个常量若不再被引用就删(本改动造成的孤儿) |
| `graph_builder/input_url_guard.py:240-274` | 手抄 URL 守卫的提示 | 改成短路径 |
| `tools/exec_view.py`、`infra/sandbox-image/runner.py` 的脚本字面量 | — | 新增:读它来挂载 |
| `inputs_doc.py`、`registry.py`、`_child_run.py`、`agent_sandbox.py:1639`、`builder.py`、`workspace_janitor.py`、`turn_inputs.py`、`agent_factory.py:1783` | 注释 | 只改明显说错的注释,不动行为 |
| `apps/admin-ui/docs-site/guide/chat.md:558`、`:657`、`:685-689` | 对外文档 | 加 `inputs/current/` 的说法;环境变量那段保留(仍然有效) |
| `apps/admin-ui/src/components/console/__tests__/PlatformRows.test.tsx:15-21` | 控制台原样显示隐藏行的测试样例 | 不用改:组件逐字显示,不解析 |
| `manifests/`、`configs/`、`platform-skills/`、`environments/` | — | 没有命中 |

### 3.3 第 3 点:守卫(白名单受检面,用户已定)

#### 3.3.1 查哪里、拦什么

**只查三处,其余一律不查**:

| 受检面 | 查哪些字符串 | 不查 |
|---|---|---|
| ① MCP 工具(名字以 `mcp__` 开头,`tools/mcp.py:88`)和 `http` 工具(`tools/http.py:116`) | 参数里**全部字符串叶子**(递归,不看字典键) | 这次被平台绑定填过的参数(`_fill_bound_args` 返回的逐调用参数名,`builder.py:1902-1927`) |
| ② 平台工具的路径参数:守卫自己维护的一张「工具名 → 路径参数名」表(`read_file` / `write_file` / `edit_file` / `list_dir` / `read_document` / `read_page` / `ask_image` 的 `path`,`save_artifact` 的 `path`;`search_files` 没有路径参数,实现时核对后去掉) | 表里这些参数的值 | 同一工具的其它参数 |
| ③ `ask_image.path` | 这个参数的值 | `question`、`image_ref` 等 |

不在表里的一律不查,包括:`exec_python` / `bash`、委派任务(`spawn_worker` / 子 Agent 的 task)、`update_plan`、技能编写(`author_skill` / `refine_skill` / `fork_skill`)、`remember`、`note_behavior_patch`、`write_file` / `edit_file` 的内容(含顶层 `edits` / `old_string` / `new_string`,它们不在路径参数表里)、`knowledge_search`、`web_search`。理由(用户已定):平台技能大量教 `$EXPERT_WORK_SKILLS_DIR/...` 的写法,在子代、在之后执行的脚本里都是有效的,拦下会把正确的路径改坏;而白名单里的三处,字面量 `$名字` 在任何情况下都不会被展开。

**命中规则**(在受检的字符串里):

1. `$名字` 或 `${名字}`,「名字」是 `$` 后**完整的**标识符(`[A-Za-z_][A-Za-z0-9_]*`,贪婪取完),且**恰好等于**一个声明过的变量名(区分大小写)。
2. `$EXPERT_WORK_` 或 `${EXPERT_WORK_` 开头的标识符。例外:受检面 ②③ 里**开头**的 `$EXPERT_WORK_INPUTS_DIR/`(或 `${…}/`)不算 —— 解析函数认它(用户定 4)。
3. **本地路径传给外部服务(审查 M3,只在受检面 ①)**:一个字符串叶子去掉首尾空白后,**整段**是一个不含空白的路径,且以 `inputs/current/`、`inputs/<UUID>/`、`inputs/cache/` 开头(前面可带 `/workspace/`)。只认整段,不认句子里的一截:`"inputs/outputs 比例"`、`"见 inputs/current/a.png"` 都不拦。

**为什么不用 `spec.path_args`**:它是调度器的冲突键(`tools/scheduling.py:59-68`),不是「这是路径参数」的声明 —— `save_artifact` 的 `path_args=("name",)`(`artifact.py:228-231`)是产物名,真正的工作区路径参数是 `path`。拿它当受检面会查错参数、漏掉真参数。守卫的表与工具定义放在一起维护,测试钉住 `save_artifact` 查 `path` 不查 `name`。

「声明过的变量名」= 本图 spec 声明的变量名(在 `agent_factory.py:1379-1382` 旁边按同样写法新增 `declared_input_names` 传进图)∪ 本次 config 里 `prompt_inputs` 的键(子代的图不一定带父的声明,但 `prompt_inputs` 是从父带下来的)。

#### 3.3.2 接在哪

照 `graph_builder/input_url_guard.py` 的写法和接入点:

- 新模块 `graph_builder/input_literal_guard.py`,判定是纯函数:`find_literal(tool_name, args, *, spec, names, skip_args) -> LiteralHit | None` 和 `literal_guard_message(hit) -> str`。
- 在 `tools_node` 里紧挨着 `_guard_sandbox_calls`(`builder.py:2127`)算一次 `literal_hits`,派发时(`:2218` 附近)与 `guard_hits` 同样处理:命中的那条不派发,其余照常。
- **在审批门之后**(用户定 6),只影响派发,不改审批门和 action screening 的判定与下标(与 URL 守卫的裁定 5 同一口径);审批续跑重算一遍,判定是确定的。
- **新计数「已批准后被字面量守卫拦下」**:审批续跑路径上(`builder.py:1984-2045` 的 `apply_resume_decision` 之后),被批准的那条调用又被字面量守卫命中时,`tools_literal_guard_blocked_after_approval_total{tool}` 加一,并打一行 `tools.literal_guard_blocked_after_approval tool=… reason=…`(不记值)。看数据再决定要不要把守卫挪到审批门之前。
- 拦下时:审计一行 `tool:blocked`,`reason=input_variable_literal`(规则 1、2)或 `reason=input_local_path_to_remote`(规则 3),details 只记变量名 / 环境变量名与参数名,不记任何值;`_tool_call_total{outcome="blocked"}` 加一;错误分类 `invalid_arguments`。
- 守卫永不让 run 失败:判定里出任何意外,放行整批、只记异常类型(与 `builder.py:4265-4280` 同一条规矩)。
- 一次调用多处命中,只报第一处(按参数在 args 里的顺序,深度优先)。

#### 3.3.3 回给模型的话

逐字见 §4.5。所有提示只含参数名、变量名、绑定参数名、环境变量名 —— 全是配置或 schema 里的名字,不是值、不来自租户数据,可以直接写。

### 3.4 第 4 点:代码工具描述

**改 `tools/sandbox.py:1107-1121`、`tools/bash.py:98-112`**:两处今天是两份逐字相同的字面量。改成一个共享常量 `INPUTS_CODE_NOTE`(放在 `tools/inputs_doc.py`,与目录、文件名常量放一起),两个工具都引用它 —— 一致性由结构保证,测试再钉一次。

保留的保护:清单在 `$EXPERT_WORK_INPUTS`、代码里从清单读、不要手抄长串、文件不在就按原地址下载、链接命名规则。改的只有路径说法:文件目录写 `inputs/current/`,注明和 `$EXPERT_WORK_INPUTS_DIR` 是同一个目录。逐字见 §4.3。

### 3.5 审查 LOW 各条怎么处理

1. **`vision.py:1439` 按扩展名分类,没有扩展名的链接名会被当成文档。** 链接名的扩展名取自 URL 路径,URL 没有扩展名时链接名就没有(`inputs_doc.py` 的 `_EXT_RE`),而缓存文件的扩展名按 content-type 定(`prefetch_script.py` 的 `pick_suffix`)。处理:`ask_image` 在 `_is_image_path` 判否、且解析后的真实路径在 `inputs/<UUID>/` 下时,先过 `resolve_input_link`,按**缓存文件**的扩展名再判一次;是图片就走工作区图那条路。测试 §9.5 L5。
2. **`list_dir("inputs/current")` 显示链接自身的 lstat 大小**(几十字节,`nas_workspace_store.py:661-666`)。处理:`NasWorkspaceStore.list_dir` 在列 `inputs/<UUID>/` 子树时,对符号链接条目套同一个 `resolve_input_link` 规则,认得出的就报缓存文件的大小,认不出的照旧报 lstat 大小。列表的遍历仍是 `follow_symlinks=False`,不改安全性质。测试 L6。
3. **`edit_file` 的豁免要写到顶层 `edits` / `old_string` / `new_string`。** 白名单之后 `edit_file` 只有 `path_args=("path",)`(`file_ops.py:1580`)受检,`edits`、`old_string`、`new_string`(`:1453-1476`、`:1558-1559`)都不在受检面里,不用另写豁免。测试 G5 钉住。
4. **声明的变量名撞上 LaTeX、GraphQL、Mongo 写法。** 白名单之后只剩 MCP / http 的字符串叶子会碰到:
   - LaTeX:`$x$` 里的 `x` 要恰好是声明的变量名才拦;健管师这类变量名(`customer_code`、`is_first_contact`)不会出现在公式里。可忽略。
   - Mongo 操作符:`$match`、`$set`、`$in`、`$gt` 只有在租户把变量也叫 `match` / `set` / `in` / `gt` 时才拦。可忽略。
   - **GraphQL**:经 `http` 工具发 GraphQL,查询里的 `$customer_code` 与声明的变量同名时**会拦**,而 GraphQL 语法要求 `$`,模型绕不开。这是白名单里唯一真实的误拦面。处理:审计和计数带工具名,能看到是否发生;发生了,租户给变量改个名即可,或把那个 GraphQL 参数改成绑定。眼下没有用 `http` 调 GraphQL 的智能体,不为它加例外。
5. **老视图里 `/agent/inputs/current/x` 的报错文案。** 解析函数直接拒,文案指回 `inputs/current/x`(§4.5)。对话视图沿用现有拒绝(`workspace_paths.py:344-351`)。
6. **`agent_key` 为空时宿主侧和沙箱侧口径不一致。** 沙箱侧:没绑智能体时 `nas_view` 走早返回,不挂。宿主侧:`inputs_run` 的表达式加上 `agent_key` 条件(§3.2.2 第 2 条),没绑时为 `None`,`inputs/current/` 报「本轮没有输入变量」同一个错。真实会出现空 `agent_key` 的只有合成评测路径(`registry.py` 中 `agent_key` 字段的说明)。
7. **「有文件」那句按能力门控。** 见 §3.1 第 7 条。
8. **`current` 桩里原来已有的文件会被挂载盖住(存疑,已确认)。** bind mount 挂在非空目录上会把原有内容遮住,这是 Linux 挂载的固定行为,不是存疑项。桩里什么时候会有东西:平台从不往桩里写;只有在**没有挂载**的 exec 里(本轮没有输入、或这次跳过了)模型写 `inputs/current/x`,才会落进 NAS 上的空桩。后果:那些文件在有挂载的 exec 里看不见,宿主侧工具永远看不见(解析函数要么改写到 run 目录,要么报「本轮没有输入」,从不读桩)。不处理,写进测试 X5 钉住行为。

---

## 四、新旧文案对照(逐字)

### 4.1 「本轮输入」段

| 位置 | 旧 | 新 |
|---|---|---|
| 标题 `HEADER` | `[本轮输入]（平台自动生成）` | 不变 |
| 第 2 行 | `输入文件目录 $EXPERT_WORK_INPUTS_DIR，清单 $EXPERT_WORK_INPUTS（exec_python / bash 里直接用）。` | **删除** |
| 第 3 行 | `代码里要用到下面任何值时，从目录或清单读；不要从上文手抄，长串抄错一位就是 404。` | **删除** |
| 有文件且能读文件时的一句(新增) | — | `本轮输入文件在 inputs/current/ 下（预先下载失败的不在）。` |
| 没传 `_UNSET` | `本轮未提供` | 不变 |
| 非文件 trusted `_TEXT` | `已提供（非文件）` | `已提供` |
| 非文件 untrusted `_UNTRUSTED_TEXT` | `已提供（非文件，外部数据，需逐字使用时从清单读）` | `已提供（外部数据）` |
| 整个值是地址,trusted | `文件 $EXPERT_WORK_INPUTS_DIR/{link}；不在则按清单里的原地址下载` | `文件 inputs/current/{link}` |
| 整个值是地址,untrusted | `$EXPERT_WORK_INPUTS_DIR 下以 {var} 开头的文件（精确文件名读清单里 {var} 的 local_path）；不在则按清单里的原地址下载` | `文件在 inputs/current/ 下，名字以 {var} 开头（确切文件名见清单里 {var} 的 local_path）` |
| 列表 | `{N} 项，每项的文件在 $EXPERT_WORK_INPUTS_DIR/{var}/<下标-说明>；不在则按清单里该项的原地址下载` | `{N} 项，其中 {M} 个文件在 inputs/current/{var}/ 下（按 <下标-说明> 命名）` |
| 对象 | `{M} 个文件在 $EXPERT_WORK_INPUTS_DIR/{var}.<字段名>；不在则按清单里该字段的原地址下载` | `{M} 个文件在 inputs/current/ 下（按 {var}.<字段名> 命名）` |
| 文件变量,智能体没有读文件的工具 | (同上各行) | `已提供`(untrusted 为 `已提供（外部数据）`) |
| 被绑定(接在状态后) `_BOUND` | `；绑定了它的工具由平台自动填，不用手抄` | `；由平台填进绑定它的工具参数` |

「不在则按清单里的原地址下载」从每行挪走了:段里只说一次「预先下载失败的不在」(事实),怎么补救写在代码工具描述里。untrusted 那行仍指向清单的 `local_path`:真实文件名带租户 URL 的扩展名,不能进段(裁定 P16),只说前缀又有歧义(B-73 ④)。

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

新(两处引用同一个常量 `INPUTS_CODE_NOTE`):

> 本轮如果有输入变量，它们在 $EXPERT_WORK_INPUTS 指向的 JSON 清单里（含原地址、编码、本地文件路径）；本轮没有输入变量时这个文件不存在，读不到就照上文办，不必重试。代码里要用到某个输入值时，从清单读，不要从上文手抄——长串抄错一位就是 404。清单里 local_path 非空表示平台已把该文件下载到本地，直接用它，不必再联网下载；极少数情况下（挂起很久之后续跑）该文件已被清理，这时改用同一项里的原始 URL 自己下载。平台预先下载的文件在 inputs/current/ 下（与 $EXPERT_WORK_INPUTS_DIR 是同一个目录）：整个值是地址的变量叫 <变量名><扩展名>，对象字段里的叫 <变量名>.<字段名><扩展名>，列表项叫 <变量名>/<下标-说明><扩展名>；确切路径见清单里该项的 local_path（值是 JSON 字符串时在 value_parsed[i].local_path）。文件不在就按清单里的原地址下载。地址一律从清单读或用这些文件，不要手抄。

(实现时核对:旧文的「local_path 非空 → 直接用、不必联网」一句是现有测试 `test_both_sandbox_tool_descriptions_state_the_local_path_fallback` 钉住的常见路径说明,保留;与旧文相比只改了「文件」→「清单」两处措辞和目录写法。)

### 4.4 手抄 URL 守卫的提示(`input_url_guard.py:240-274`)

| | 旧 | 新 |
|---|---|---|
| trusted | `这个文件应在 $EXPERT_WORK_INPUTS_DIR/{link}（不在则按清单里的原地址下载）；请改用它，或用代码从 $EXPERT_WORK_INPUTS 清单里读 {var} 的原地址，不要手抄。` | `这个文件应在 inputs/current/{link}（不在则按清单里的原地址下载）；请改用它，或用代码从 $EXPERT_WORK_INPUTS 清单里读 {var} 的原地址，不要手抄。` |
| untrusted | `这个文件应在 $EXPERT_WORK_INPUTS_DIR 下，确切文件名见 $EXPERT_WORK_INPUTS 清单里 {var} 对应条目的 local_path（不在则按清单里的原地址下载）；请用代码从清单里读路径或原地址，不要手抄。` | `这个文件应在 inputs/current/ 下，确切文件名见 $EXPERT_WORK_INPUTS 清单里 {var} 对应条目的 local_path（不在则按清单里的原地址下载）；请用代码从清单里读路径或原地址，不要手抄。` |

这条守卫只拦 exec_python / bash 的代码,那里命令式的话是对的,只改路径说法。

### 4.5 新守卫和解析报错(全部新增)

`{arg}` 参数名,`{var}` 变量名,`{param}` 绑定参数名,`{name}` 环境变量名,`{tool}` 工具名,`{path}` 模型传的原样路径。

| 情况 | 文案 |
|---|---|
| 守卫 ①:`$变量名`,且这个变量绑定在本工具的某个参数上 | `[blocked] 参数 {arg} 里的 ${var} 不会被换成值，收到的就是这串字。{var} 由平台自动填进本工具的 {param} 参数（它不在你看到的参数表里），这里不用写，去掉后重新调用。` |
| 守卫 ①:`$变量名`,其它情况 | `[blocked] 参数 {arg} 里的 ${var} 不会被换成值，对方服务收到的就是这串字。请写出要传的实际内容；如果 {var} 由平台填进绑定它的工具参数，就不用写。` |
| 守卫 ①:`$EXPERT_WORK_*` | `[blocked] 参数 {arg} 里的 ${name} 只在 exec_python / bash 里有值，对方服务收到的就是这串字。` |
| 守卫 ①:整段是 `inputs/…` 本地路径(M3) | `[blocked] 参数 {arg} 是工作区里的本地路径，对方服务读不到沙箱里的文件。要传文件地址，请用绑定（由平台填）或原始链接。` |
| 守卫 ②③:`$变量名` | `[blocked] 参数 {arg} 里的 ${var} 不会被换成值。要用 {var} 的文件，写 inputs/current/ 下的路径（见「本轮输入」段）。` |
| 守卫 ②③:`$EXPERT_WORK_INPUTS` / 非开头的 `$EXPERT_WORK_INPUTS_DIR` | `[blocked] 参数 {arg} 里的 ${name} 只在 exec_python / bash 里有值。要用本轮输入文件，写 inputs/current/ 下的路径。` |
| 守卫 ②③:`$EXPERT_WORK_SKILLS_DIR` | `[blocked] 参数 {arg} 里的 ${name} 只在 exec_python / bash 里有值。读技能文件用 skill_view。` |
| 守卫 ②③:其它 `$EXPERT_WORK_*` | `[blocked] 参数 {arg} 里的 ${name} 只在 exec_python / bash 里有值。` |
| 解析:`inputs/current/…`,本轮没有输入 | `{tool}: inputs/current/ 是本轮输入文件的目录；本轮没有输入变量，里面没有文件。之前几轮的文件用当时工具结果或渲染页段里给出的路径: {path!r}` |
| 解析:老视图里 `/agent/inputs/current/…` | `{tool}: inputs/current/ 只在 /workspace 下有效，请写 inputs/current/<文件名>: {path!r}` |

---

## 五、跨场景对照表

| # | 场景 | 改完后模型看到什么 / 发生什么 | 为什么不会坏 |
|---|---|---|---|
| a | 纯对话,用了短编号(健管师:身份编号绑给 MCP,`is_first_contact` 进 `if`) | 段里每个变量「已提供」,被绑的加「由平台填进绑定它的工具参数」;没有文件句,没有任何命令 | 编号的值照旧由 `arg_bindings` 填进工具(`arg_bindings.py:212`,不改);模板渲染非地址值不改;段里不再出现「要用值就去读清单」。万一仍把 `$customer_code` 写进 MCP 参数,守卫 ① 拦下并说明由平台填 |
| b | 文档分析:模板渲染了上传文件的链接,要用代码处理 | 提示词里 `inputs/current/report.pdf（本轮输入文件；预先下载失败时不在）`,段里「文件 inputs/current/report.pdf」 | exec_python `open("inputs/current/report.pdf")` 经挂载打开;`read_document` / `read_page` 经解析函数读 `inputs/<run>/report.pdf`;`ask_image` 再经链接解析读到缓存文件。下载失败时代码工具描述说按清单原地址下载;`$EXPERT_WORK_INPUTS_DIR` 仍可用 |
| b2 | 场景 b 多轮追问,**第二轮有新输入**(又一份 `report.pdf`) | 第二轮 `inputs/current/report.pdf` 是新的那份;渲染页段里上一轮读过的页给的是真实路径 `inputs/<第一轮>/report.pdf` | `figure_documents` 记的是真实路径(H1),下一轮按任何 view 算出的 `doc_sha` 都是第一轮那份;两份同名文档各有各的 `doc_sha`(按 `rel` 算),不会互相顶替 |
| b3 | 场景 b 多轮追问,**第二轮没有输入** | 模型若再写 `inputs/current/report.pdf`,得到「本轮没有输入变量……之前几轮的文件用当时工具结果或渲染页段里给出的路径」 | 第一轮的 run 目录(清单和链接)保留 30 天,但链接指向的缓存文件只保留 7 天(`workspace_janitor.py:173`、`:186`),过期后链接失效、要按清单原地址重下;而且同一 URL 重新下载时会覆盖同扩展名的旧缓存,新鲜度判断看的又是链接本身的 lstat(`prefetch_script.py:297-317`、`figure_lookup.py:375-384`)—— 这些是今天就有的行为,本方案不改,只是不把「仍在」说满。在保留期内,渲染页段给的真实路径照样能用;沙箱里本轮不挂,`inputs/current/` 是空桩 |
| c | 附件只绑给工具 | 段里:是地址字符串就「文件 inputs/current/x.jpg；由平台填进绑定它的工具参数」,是裸字符串数组(健管师 `attachment_urls`,不预下载,`inputs_doc.py:317-352`)就「已提供；由平台填进…」 | 绑定填值不改;文件那句是事实;工具参数已从 schema 删掉。写了 `$x` 或把 `inputs/current/x.jpg` 填进 MCP 参数,守卫 ① 拦并说「用绑定或原始链接」 |
| d | 附件绑给工具,同时要在代码里分析 | 段里同 c;代码里用 `inputs/current/x.jpg` 或读清单 | 两条路互不影响:绑定走 `prompt_inputs`,代码走挂载 / 清单,都指同一轮 |
| e | untrusted 文本 | 提示词里照旧围栏;段里「已提供（外部数据）」;untrusted 地址只说「名字以 {var} 开头」 | 段里仍不写值、不写由值推出的名字(零值测试钉住);守卫提示只含名字,不必围栏 |
| f | 用循环渲染链接列表(trusted 真 list) | `{% for m in materials %}{{ m.url }}` 每项得到 `inputs/current/materials/0-示范视频.mp4` | 结构副本只换路径前缀,下标、遍历、`length` 不变(`prompt_render.py:132-157`);挂载点与 `<run_id>/` 同深度,嵌套链接 `../../cache/x` 照常解析 |
| g | 变量只在 if 条件里出现 | 段里「已提供」 | 非地址值的模板上下文不变;本方案不判断「变量进没进提示词」,没有误判可言。剩下的风险是模型出于好奇去读值,见 §十一 风险 5 |
| h | 非 jinja 模板 | 无段;不收 inputs(`prompt_render.py:275-278`,422) | 没有 inputs → 不写清单、不注入环境变量 → 脚本取不到 run id 不挂;代码工具描述那段以「本轮如果有输入变量」开头;守卫只剩 `$EXPERT_WORK_` 和本地路径两条,在受检面里写它们本来就不会生效 |
| i | 可选变量本轮没传 | 「本轮未提供」(被绑也是,P12) | 模板拿空串(不改);绑定把参数删掉(不改)。若所有变量都没传 → 没有清单 → `inputs_run` 为 `None`,同 b3 |
| j | 委派的子代 | 子代没有自己的段;父可以在任务里写 `inputs/current/x` | 子代 `view.inputs_run` 与环境变量都是父那一轮首段(§3.2.4);委派任务文字不在受检面,父写 `$EXPERT_WORK_SKILLS_DIR/...` 这类教给子代的有效写法不会被拦 |
| k | 没有任何输入变量 | 无段 | `build_inputs_block` 返回 `None`(不改);其余同 h |
| l | 模板里自己写了 `$EXPERT_WORK_INPUTS_DIR` | 提示词原样带着这串(租户文字不改写) | 代码工具里环境变量值不变;交给路径参数时解析函数当别名,落盘写真实路径;写进 MCP 参数时守卫 ① 拦 |
| m | 审批续跑 / regenerate / 压缩 | 续跑的 `inputs_run_id` 指回首段(`turn_inputs.py`);段按标重放、压缩后放回 | `inputs_run` 跟着 `inputs_run_id` 走,环境变量与挂载仍是首段的目录;段的标记和构造不改 |
| n | `render: raw` 的变量(值是地址) | 提示词里是原地址(trusted)或围栏里的原地址(untrusted),不改写;段里仍有「文件 inputs/current/x」 | `render_value` 对 `raw` 早返回(`prompt_render.py:202-203`,不改);预下载按 `linked_sites` 照做,与 render 无关,所以文件那行是真的。原地址留给模型填进没绑定的工具,守卫不查 URL |
| o | 没有任何读文件工具的智能体(现实里不存在,`exec_python` 恒装;结构上要成立) | 段里文件变量写「已提供」,没有文件句 | §3.1 第 7 条的门;其余行为与 a 相同。系统提示词里的文件变量仍渲染成 `inputs/current/…`(渲染层不看工具表)。眼下 `exec_python` 每个智能体都装,这个分支走不到 |
| p | 多轮追问且本轮没有输入,模型拿上一轮的回执路径再调工具 | 同一轮里回执显示的是 `inputs/current/…`;跨轮的渲染页段显示真实路径 | 本轮没有输入时,上一轮回执里的短路径会报「本轮没有输入」并指向渲染页段。**本轮若有同名的新输入**,历史回执里的短路径会**静默**指向新文件 —— 与风险 4 一致,和今天 `$EXPERT_WORK_INPUTS_DIR` 的行为相同;跨轮要用的路径(渲染页段)给的是真实路径,不受影响 |
| q | 后台任务、子代跨过了父对话的下一轮 | 子代 / 后台任务仍挂、仍解析到父**派它那一轮**的目录 | 这是对的:它拿到的 `prompt_inputs` 也是那一轮的,绑定填的是那一轮的值,文件必须同一轮才对得上;那一轮的 run 目录保留 30 天(缓存文件 7 天,同 b3),与今天环境变量的口径一样 |

---

## 六、守卫的误伤分析(白名单之后)

受检面只有 MCP / http 的字符串叶子、平台工具的路径参数、`ask_image.path`。

| 文本 | 在哪 | 拦不拦 | 理由 |
|---|---|---|---|
| `$100`、`$5.99`、`US$20`、`$ 100` | MCP / http | 不拦 | `$` 后不是字母或下划线,取不出标识符 |
| `$USD`(没有叫 `USD` 的变量) | MCP / http | 不拦 | 只认声明过的名字 |
| 声明了 `customer_code`,文本里 `$customer`、`$customer_code_v2`、`$Customer_code` | MCP / http | 不拦 | 标识符贪婪取完再比,整个相等;区分大小写 |
| `${customer_code}` | MCP / http | 拦 | 同一个意思的另一种写法 |
| 声明了 `price`,MCP 自由文本里本来就要写 `$price` | MCP | **会拦** | 只有租户变量名恰好出现在 `$` 之后才发生;提示让模型写实际内容 |
| GraphQL 查询里的 `$customer_code`(与声明同名) | http | **会拦** | §3.5 第 4 条:唯一真实的误拦面,靠审计看、改名或改绑定解决 |
| LaTeX `$x$`、Mongo `$match` | MCP / http | 几乎不会 | 要租户变量恰好叫 `x` / `match` |
| `"inputs/outputs 比例"`、`"见 inputs/current/a.png"` | MCP / http | 不拦 | 规则 3 只认整段路径 |
| `"inputs/current/a.png"` 整段 | MCP / http | 拦 | 对方读不到 |
| 被平台绑定填进去的值(含 `$…` 或 `inputs/…` 字样) | MCP / http | 不拦 | 绑定参数排除,那是租户数据 |
| exec_python / bash 代码里的任何写法 | — | 不查 | 不在受检面 |
| 委派任务、`update_plan`、技能编写、`remember`、`note_behavior_patch` 里的 `$EXPERT_WORK_SKILLS_DIR/...` | — | 不查 | 不在受检面;这些写法在子代和之后执行的脚本里有效 |
| `write_file` / `edit_file` 的内容(`content`、`edits`、`old_string`、`new_string`) | — | 不查 | 不在路径参数表里 |
| `knowledge_search` / `web_search` 的查询 | — | 不查 | 不在受检面 |
| `ask_image(path="$EXPERT_WORK_INPUTS_DIR/x.png")` | ③ | 不拦,并且读得到 | 开头别名由解析函数认 |
| `read_file(path="$EXPERT_WORK_SKILLS_DIR/a/SKILL.md")` | ② | 拦,提示用 skill_view | 今天这条本来就读不到,现在提示更准 |
| `read_document(path="$report")` | ② | 拦,提示写 `inputs/current/` 下的路径 | 今天会报「文件不存在」,现在说清原因 |

**M3 已知会漏拦的写法**(规则 3 只认整段以 `inputs/…` 或 `/workspace/inputs/…` 开头的路径,下面这些不认,接受):

- `./inputs/current/x`(前面带 `./`);
- `file:///workspace/inputs/…`(URL 形式);
- Markdown 图片写法 `![](inputs/current/x.png)`、句子里夹带的路径;
- JSON 字符串里嵌的路径(参数值本身是一段 JSON 文本,如 `"{\"img\": \"inputs/current/x\"}"`)。

这些写法漏过去的后果是对方服务收到一个打不开的路径、报错或忽略 —— 与今天相同,不会更坏。要收得更紧就得在句子里找路径,误伤自由文本,不划算。

开销:受检的字符串每条扫一遍,一个正则找 `$` 标识符再查集合,外加一次整段前缀比较;线性,不需要预算。

---

## 七、兼容和迁移

- **两个环境变量在绑了智能体的 run 上照旧注入、值不变**(`/workspace/inputs/<run_id>/inputs.json` 和它的目录)。没绑智能体的 run(`agent_key` 为空,只有合成评测路径)因为 `inputs_run` 多了 `agent_key` 条件,**不再注入**这两个变量 —— 那条路径本来就没有租户、不写清单,注入的是悬空路径。已有模板、技能、第三方代码里写 `os.environ["EXPERT_WORK_INPUTS"]`、`$EXPERT_WORK_INPUTS_DIR/x` 的,在代码工具里继续可用。
- **清单 `local_path` 不变**,仍是 `inputs/<run_id>/…`(相对 `/workspace`),它是代码读的,不经模型手抄。
- **落盘的路径一律是真实路径**(H1):`figure_documents`、渲染页 ref、ask_image 元数据、审计。发布前已经记下的 `figure_documents` 里没有短路径(那时还没有这个写法),不用迁移。
- **旧会话里的旧段**:历史里留着写 `$EXPERT_WORK_INPUTS_DIR/…` 的旧段。代码工具里照旧可用;路径参数里由别名接住;压缩后只放回最新一段(新格式)。
- **租户模板**:仓库内 `manifests/`、`configs/`、`platform-skills/` 没有命中。线上租户模板里如果写了,见场景 l。
- **对外文档**(`chat.md:558`、`:657`、`:685-689`):加 `inputs/current/` 的说法,说明它和 `$EXPERT_WORK_INPUTS_DIR` 是同一目录;提示词里地址被换成的本地路径改成新写法。
- **提示词缓存**:有地址变量的 jinja 智能体,系统提示词的字节在发布那一刻变一次(路径和尾注),之后每轮照旧稳定。
- **NAS 上多出的 `inputs/current/` 空目录**:只在有 run 目录时才建;回收任务不碰(不是 UUID 名),控制台不列(保留前缀)。回滚后它就是个空目录,无害。
- **沙箱镜像**:测试、生产(ACS)不用重建;本地后端重建本地沙箱镜像后才有挂载,没重建时 `$EXPERT_WORK_INPUTS_DIR` 照旧可用。
- **没有数据库变更,不用回填。**
- **回滚**:回退代码即可。回滚后 `$EXPERT_WORK_INPUTS_DIR` 仍有效,`inputs/current/` 失效(只影响回滚窗口里的会话);落盘的都是真实路径,回滚后照旧可用。

---

## 八、别名为什么只接路径参数开头的 `$EXPERT_WORK_INPUTS_DIR/`(用户已定)

它有两个正当来源:会话历史里的旧段(发布前每一轮都这么写)、租户自己写的模板。拦下要多一轮,而它的意思毫无歧义。别名只认这一个、只认开头;归一成短路径给模型看,落盘写真实路径。路径中间出现、`$EXPERT_WORK_INPUTS`(清单文件)、`$EXPERT_WORK_SKILLS_DIR` 照拦。

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
| B12 | `reads_files=False`:有地址变量也没有文件句,文件变量写 `已提供`;`runs.py` 按 `tool_catalog` 算这个开关(一份有 `exec_python` 的目录 → True,只有 MCP 工具的目录 → False) | 门没接上;开关算反 |

### 9.2 提示词渲染(`services/control-plane/tests/test_prompt_render.py`)

| # | 测试 | 改坏哪一处会红 |
|---|---|---|
| R1 | 字符串地址(trusted / untrusted)渲染出 `inputs/current/{link}`,全文不含 `EXPERT_WORK` | `_render_url`(`:181`)没改 |
| R2 | JSON 字符串列表 / untrusted 列表的逐项块用短路径 | `_render_items`(`:118`)没改 |
| R3 | trusted 真 list:`{% for m in materials %}{{ m.url }}{% endfor %}` 每项是短路径;`{{ materials \| length }}` 不变 | `render_value` 结构副本(`:217`)没改 |
| R4 | 尾注逐字等于新 `URL_NOTE`,不含「下载」二字 | 尾注回退 |
| R5 | 非 jinja 智能体输出逐字等于存储的提示词;`render: raw` 变量仍给原地址 | 改动漏到非 jinja 路径或 raw 分支 |

### 9.3 短路径解析(`services/orchestrator/tests/test_workspace_paths.py` 等)

| # | 测试 | 改坏哪一处会红 |
|---|---|---|
| P1 | `inputs/current/x.png`、`/workspace/inputs/current/x.png` → `rel == path == "inputs/<R>/x.png"`,`display == "inputs/current/x.png"` | 改写缺失;`path` 留了短路径(H1) |
| P2 | `$EXPERT_WORK_INPUTS_DIR/x.png`、`${EXPERT_WORK_INPUTS_DIR}/x.png` → `path` 是真实路径,`display` 归一成 `inputs/current/x.png`;`a/$EXPERT_WORK_INPUTS_DIR/x` 不改写 | 别名缺失;别名不限开头;别名落盘写了别名 |
| P3 | `inputs_run is None` 时 `inputs/current/x` 报 §4.5 的文案 | 静默放行成 `inputs/current/x`(读到空桩) |
| P4 | `inputs/<uuid>/x`、`inputs/cache/x`、`inputs/currentx/y` 与今天结果相同;老视图 `/agent/inputs/current/x` 报 §4.5 的文案;对话视图 `/agent/inputs/…` 仍是现有拒绝 | 改写范围过宽;老视图没拦 |
| P5 | 对话视图:`agent_relative` 得到 `threads/<conv>/work/inputs/<R>/x` | 对话视图下拼错前缀 |
| P6 | `ToolContext.workspace_view.inputs_run`:主 run = `run_id`;有 `inputs_run_id` 用它;`prompt_inputs` 为空或 `agent_key` 为空时 `None`;`_child_config` 出来的子代 = 父首段。现有 `test_exec_python_tool.py:33-34` 的 `_ctx()` 不带 `agent_key`,要补上(否则 `:66-78`、`:93-105` 两条「有 inputs 就传 run id」的断言会因为新条件拿到 `None`);另加一条没绑智能体 → `None` | 子代用了自己的 `sub_run_id`;没 inputs / 没绑智能体也给了值 |
| P7 | **H1 多轮**:第 1 轮 `read_page("inputs/current/report.pdf")` 写进 state 的 `figure_documents` 值是 `inputs/<R1>/report.pdf`;第 2 轮(有新输入 R2 / 没有输入两种)用第 2 轮的 view 跑 `figure_block._document_path`,两种都返回第 1 轮的真实路径 | `figure_documents` 记了短路径 → 第 2 轮返回 `None` 或指到 R2 |
| P8 | 两轮同名 `inputs/current/report.pdf` 的 `doc_sha` 不同 | `rel` 没换成真实路径 |
| P9 | `ask_image` 结果元数据的 `path` 是真实路径;回给模型的文本用短路径 | `display` / `path` 用反 |
| P10 | `run_in_sandbox` 传给 `client.exec` 的 run id 来自 `view.inputs_run`;`agent_key` 为空时传 `None`(不注入环境变量) | 两处各算各的、走散 |
| P11 | `read_page("inputs/current/report.pdf")` 这一对:回给模型的回执文本里是 `inputs/current/report.pdf`(display),同一次结果的 `state_updates["figure_documents"]` 值是 `inputs/<R>/report.pdf`(path) | `display` 与 `path` 用反或用成同一个 |

### 9.4 沙箱挂载(`test_exec_view.py`、`test_exec_view_kernel.py`)

| # | 测试 | 改坏哪一处会红 |
|---|---|---|
| X1 | 契约:`inputs_doc.inputs_abs_dir(R)` 的形状恰好被脚本的 `case` 接受,并取出 `str(R)`;脚本里的 `/workspace/inputs/` 前缀与 `EXEC_VIEW` + `WORKSPACE_INPUTS_DIR` 相等 | 任一边改了目录名或前缀 |
| X2 | 环境变量缺失、为空、是 `/workspace/inputs/../x`、`/workspace/inputs/<UUID>/y`、大写 UUID、36 位但含 `/` 的串(`aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaa/aaa`)→ 不挂、exit 0、stderr 为空;脚本文本里 UUID 段不含 `*` | 校验过宽(`case` 用了 `*`);或形状不对时报错退出 |
| X2b | 绕过 shell 直接给内嵌 python 一个过了形状但不是规范 UUID 的 run id(如带大括号或无横杠的 32 位)→ 不挂 | python 侧的 `str(UUID(x)) == x` 被删 |
| X2c | 让 `bind` 失败(把 `open_tree` 换成抛 `OSError` 的桩,或在不允许挂载的环境里跑)→ exec 成功、stderr 为空、没挂 | `bind` 没包 `try`,异常让 exec 失败 |
| X3 | 真命名空间:对话视图、老视图各一例,exec 里 `open("inputs/current/org_logo.png")` 读到缓存里的字节;`inputs/current/materials/0-x.mp4`(两层链接)也读得到 | 挂载缺失;挂到了 `/workspace/inputs` 上把 `../cache` 弄断 |
| X4 | 同一工作区两个 run 并发 exec,各自的 `inputs/current` 是各自的 run | 用了平铺链接或共享符号链接 |
| X5 | run 目录不在 → 不挂、**stderr 为空**、`inputs/current` 没有被建出来;`current` 是符号链接 / 普通文件 → 不挂、stderr 为空、exec 成功;`current` 桩里原有文件在有挂载的 exec 里看不见 | 跳过时写了 stderr(M2);每次都建桩;跟着符号链接挂到了别处 |
| X6 | 载荷里 `umount inputs/current` 失败 | 挂载放到了载荷所在的 user namespace |
| X7 | `test_exec_view_script_matches_the_sandbox_image`(两份脚本相等)、`freeze` 用例照旧;挂载在 freeze 后仍可写 | 只改了一份脚本;新挂载没记进 `OURS` 被冻成只读 |
| X8 | 没绑智能体(`root` 为空)时即使环境变量在也不挂 | 挂载放到了早返回之前 |

### 9.5 宿主侧读链接(`test_nas_workspace_store.py`、`test_multimodal*.py`、`test_vision*.py`)

| # | 测试 | 改坏哪一处会红 |
|---|---|---|
| L1 | 协调方探针(临时目录里的 `l1probe/test_l1_probe.py`)作为正式测试提交进仓库:`ask_image(path="inputs/current/org_logo.png")`,链接 `-> ../cache/<32hex>.png`。先确认今天是红的(越界拒绝),修好后变绿 | `resolve_input_link` 没接进 `ask_image` |
| L2 | `resolve_input_link` 把 `inputs/<R>/x.png -> ../cache/<32hex>.png`、`inputs/<R>/m/0-a.png -> ../../cache/<32hex>.png` 解析成 `inputs/cache/<32hex>.png` | 只处理了一层 |
| L3 | 指向 `../../../etc/passwd`、`../cache/sub/x`、`../other/x`、`../cache/notHex.png`、绝对路径的链接 → 原样返回,随后照旧被拒 | 目标校验放宽 |
| L4 | `inputs/<R>/` 之外的符号链接不解析 | 范围放宽到整个工作区 |
| L5 | 链接名无扩展名、缓存文件是 `.png`:`ask_image(path="inputs/current/org_logo")` 按图片处理,不要求 `unit` | `vision.py:1439` 只看了链接名的扩展名 |
| L6 | `list_dir("inputs/current")` 里链接条目的大小等于缓存文件大小;认不出的链接仍报 lstat 大小 | 列表没套链接规则;或改成了跟随遍历 |

### 9.6 守卫(`services/orchestrator/tests/test_input_literal_guard.py` + 接线测试)

| # | 测试 | 改坏哪一处会红 |
|---|---|---|
| G1 | MCP 调用非绑定参数里有 `$attachment_urls` → 不派发,消息含绑定参数名 | 守卫没接进 `tools_node`;命中后仍派发 |
| G2 | §六里 MCP / http「不拦」的每一行各一例 | 标识符没贪婪取完;数字 / 空格当成名字;大小写不分;规则 3 认了半截路径 |
| G3 | `${customer_code}` 拦;MCP 参数整段 `inputs/current/a.png`、`/workspace/inputs/<UUID>/a.png` 拦,文案是 M3 那句 | 只认 `$name`;规则 3 缺失 |
| G4b | `save_artifact` 的 `path` 是 `$org_logo` → 拦;`name` 是 `$org_logo` → 不拦 | 受检面 ② 用了 `spec.path_args` |
| G4 | 不在受检面的工具(`exec_python`、`bash`、`spawn_worker` 的 task、`update_plan`、`author_skill`、`remember`、`note_behavior_patch`、`knowledge_search`、`web_search`)含 `$customer_code` / `$EXPERT_WORK_SKILLS_DIR/x` → 不拦 | 受检面写成黑名单 / 默认全查 |
| G5 | `write_file` 的 `content`、`edit_file` 的顶层 `edits` / `old_string` / `new_string` 含 `$org_logo` 不拦;同一调用的 `path` 是 `$org_logo` 拦 | 受检面扩到非 `path_args` 参数 |
| G6 | 绑定参数里填进去的值含 `$customer_code` / `inputs/current/x` 不拦 | 没排除绑定参数 |
| G7 | 路径参数开头 `$EXPERT_WORK_INPUTS_DIR/` 不拦;中间出现拦 | 别名例外过宽 / 缺失 |
| G8 | 路径参数里 `$EXPERT_WORK_SKILLS_DIR` 的提示含 `skill_view`;MCP 的提示不含 `inputs/current` | 提示分支错 |
| G9 | 子代(只有 `prompt_inputs`,图里无声明)的 MCP 调用写 `$org_logo` 也拦 | 名字集合只取了本图声明 |
| G10 | 审计 `reason=input_variable_literal` / `input_local_path_to_remote`,details 只有名字,不含任何参数值 | 把 args 原样记进审计 |
| G11 | 判定函数抛异常 → 整批放行,只记异常类型 | 异常冒出去让 run 失败 |
| G12 | 审批续跑:被批准的那条命中 → 不派发,`tools_literal_guard_blocked_after_approval_total` 加一;没批准过的命中不加这个数 | 计数缺失或数错了地方 |

### 9.7 工具描述(`test_exec_python_tool.py`、`test_bash_tool.py`)

| # | 测试 | 改坏哪一处会红 |
|---|---|---|
| D1 | 两个描述都包含同一个 `INPUTS_CODE_NOTE` | 一边改了另一边没改 |
| D2 | 常量里有 `$EXPERT_WORK_INPUTS`、`不要从上文手抄`、`inputs/current/`、`local_path`、`原地址下载` | 保护被删、路径说法没改 |
| D3 | 手抄 URL 守卫的两条提示含 `inputs/current/`、不含 `$EXPERT_WORK_INPUTS_DIR` | `input_url_guard.guard_message` 没改 |

### 9.8 真栈验收(测试环境,改完再跑)

1. 健管师按 B-314 测评脚本重跑同一组对话(或至少前 50 轮):`exec_python` 为读输入值的调用 ≤ 2 次(原 19 次);`$变量名` 字面量到达 MCP 的次数 = 0;守卫命中次数和「已批准后被拦」次数如实记下。
2. 一个文档分析智能体(trusted PDF 地址 + 素材列表):`read_document("inputs/current/report.pdf")`、`ask_image(path="inputs/current/materials/0-…jpg")`、`exec_python` 打开同一个文件,三条都成功。
3. 场景 b2、b3:第二轮有新输入 / 没有输入,渲染页段的路径都指第一轮那份。
4. 一个委派场景:子代用 `inputs/current/` 读到父那一轮的文件。
5. 同一轮里审批挂起后续跑:续跑段的 `inputs/current/` 仍是首段的文件。

跑之前按评测要算钱的规矩先估费用、只跑受影响的用例。

---

## 十、工作量(按审查意见重估)

| 项 | 人天 |
|---|---|
| 第 1 点:段文案、事实句、绑定说法、能力门控 + 测试 B1-B12 | 0.5 |
| 第 2 点宿主侧:`WorkspaceView.inputs_run`、解析改写、别名、老视图拒绝、`path` / `display` 分开并逐个核对使用点(H1)+ 测试 P1-P10 | 1.25 |
| 第 2 点沙箱侧:exec 脚本读环境变量与挂载(两份字面量)、静默跳过 + 测试 X1-X8(含真命名空间用例) | 1 |
| 第 2 点链接解析:`resolve_input_link`、`ask_image` 接入与扩展名判定、`list_dir` 大小 + 测试 L1-L6 | 0.75 |
| 第 2 点提示词渲染 + 测试 R1-R5 | 0.25 |
| 第 3 点守卫:白名单受检面、本地路径规则、接线、审计、批准后计数 + 测试 G1-G12 | 1.25 |
| 第 4 点描述常量、URL 守卫提示 + 测试 D1-D3;对外文档、注释 | 0.25 |
| exec 视图脚本单独安全审查 + 其余审查往返 | 0.75 |
| 真栈验收 | 0.75 |
| **合计** | **约 7 人天**(6.5~7) |

---

## 十一、风险

1. **exec 视图脚本是安全关键代码**(经过多轮安全审查)。新挂载按现有规矩按 fd 打开、`O_NOFOLLOW`、失败只跳过不跟随;由协调方安排单独的安全审查。
2. **挂载依赖一个环境变量的格式。** 格式由 `inputs_abs_dir` 定,脚本侧用 `case` 精确匹配,契约测试 X1 钉两边;改目录名而不改脚本,X1 红。格式不对时的失败方向是「不挂」,不是挂错。
3. **`WorkspaceView` 多一个字段**,它的相等 / 哈希如果被当成缓存键或锁键,会多出不该有的区分。实现前先 grep 所有用到 `WorkspaceView` 相等或哈希的地方;有的话把 `inputs_run` 设成 `compare=False`。
4. **「current」永远指本轮。** 同一轮里给模型看的回执用短路径;会话历史里上一轮的回执、旧段写的 `inputs/current/x` 到了下一轮指向新的一轮(或报「本轮没有输入」)。这和今天 `$EXPERT_WORK_INPUTS_DIR` 一样;跨轮要用的路径(渲染页段)给的是真实路径,不受影响。
5. **模型仍可能出于好奇去读值**(场景 g)。命令删了,`is_first_contact：已提供` 本身不再引导,但模型可能想知道值。真栈验收第 1 条量化;若仍超标,再考虑写「值不在本段」之类的事实,不加命令。
6. **守卫误伤**:见 §六,白名单之后只剩 MCP / http 里「租户变量名恰好出现在 `$` 之后」一类,GraphQL 是其中唯一真实的情形。代价是多一轮。
7. **守卫在审批门之后**:需要审批的 MCP 调用里写了 `$变量名`,人先被问到,批准后才被拦。用「已批准后被拦」计数看实际发生多少,再决定是否前移。
8. **`ask_image` / `list_dir` 链接解析多一次 NAS 读**(`readlinkat`),只在 `inputs/<UUID>/` 下发生,可以忽略。

---

## 十二、决定记录与剩余待定点

第 1 版的 6 点用户已于 11-18 全部定下(见开头的修订表)。第 2 版新增的两条取舍(下面第 1、2 条)复审时已按推荐定下;第 3、4 条按推荐写入:

1. **同一轮的回执用短路径,跨轮的渲染页段用真实路径。** 协调方转述的要求是「只在给模型看的文本里用短路径」。渲染页段也是给模型看的,但它是跨轮的:如果也写短路径,下一轮它就指向新的一轮,正是 H1 要防的错。所以只在**本次工具调用**回给模型的文本里用短路径,渲染页段照旧显示 `figure_documents` 里的真实路径(带 run id)。**已定。**
2. **能力门控连每行的文件路径一起关**,不只是关文件句。没有读文件工具时路径对模型没用,这样与「平台提示词跟着能力走」一致。眼下 `exec_python` 恒装,这道门实际上恒开。**已定。**
3. (已按推荐写入,不必再定)环境变量形状不对时静默不挂,而不是让 exec 失败:这是方便性的挂载,不是安全边界。
4. (已按推荐写入,不必再定)GraphQL 同名误拦不加例外,靠审计观察。
