# 工作区可见性 实施计划(B-84 PR-2 / PR-2b)

> **给执行者:** 逐 task 做,每个 task 自带测试与提交。
>
> **Spec:** `docs/superpowers/specs/2026-09-20-workspace-visibility-design.md` —— 计划从 spec 论证而来,两份一起读。

**目标:** 让模型知道工作区里已经有什么,并且查得到 —— 治掉「看不见就重打」这条实测每 60 天烧掉约 6,002 秒的路径。

**架构:** 两个 PR。PR-2b 把只读文件操作从沙箱挪到宿主 NAS(顺带新增 `search_files`);PR-2 在系统提示词里加一个 run 起点的工作区树形摘要块,读路径复用 PR-2b。**PR-2b 必须先做** —— 它是 PR-2 的地基。

**技术栈:** Python 3.12 / asyncio / `NasWorkspaceStore`(已有 `openat` 链 TOCTOU 安全原语)/ pytest。

---

## Global Constraints

抄自 spec,每个 task 的要求都隐含包含本节。

* **只改读。** `list_dir` / `read_file` 改走宿主 NAS,新增 `search_files`。**`write_file` / `edit_file` 一律仍走沙箱** —— 它们与 B-60 的私有 `/w` 挂载空间和工作区写锁绑着。
* **不新造路径解析。** 复用 `NasWorkspaceStore` 既有的 `_normalize_workspace_path` + `_open_parent_dir_fd`(`openat` 链,TOCTOU 安全)。`resolve_scope` 的语义要逐条对照,不是照感觉重写。
* **判据比 `(st_dev, st_ino)`,不比路径字面量。** 我们在 cwd 比较上同一个文件里错过三次:`getcwd(2)` 返回 realpath,而挂载点是 symlink。
* **symlink 不 resolve 出根。**
* **不留开关。** 直接替换沙箱读路径,不做「可回落」的旋钮 —— 开关会有人忘记,而忘了之后静默跑偏比统一的坏更糟。
* **NFS 一致性结论以实测为准。** 实测跨客户端 12~15ms 可见;挂载参数里既无 `noac` 也无显式 `actimeo`,按 NFSv3 默认推断会得出 30~60 秒的错误结论。**这句要写进代码注释**,否则下一个人会照理论改回去。
* **ruff RUF001/RUF002 禁止 Python 的 docstring/注释里出现全角标点**(`,` `:` `(` `)` `;`),用半角;中文正文可以。
* 本地测试 `uv run --no-sync pytest <路径> -q`;worktree 里必须带 `UV_PROJECT_ENVIRONMENT` 且必须有 `--no-sync`。
* **每条新断言必须变异自证**:改回旧行为 → 对应断言转红 → 还原 → 转绿;还原后 `git diff --stat` 确认干净并 grep 被改的符号还在。

---

## 文件清单

| 文件 | 职责 | 动它的 task |
|---|---|---|
| `services/orchestrator/src/orchestrator/tools/workspace_store.py` | `WorkspaceFileEntry` / `WorkspaceStore` 协议 | 1 |
| `services/orchestrator/src/orchestrator/tools/nas_workspace_store.py` | NAS 实现(安全原语在这) | 1, 2, 3 |
| `services/orchestrator/src/orchestrator/tools/workspace_store.py` | 另外两个实现 `SupervisorWorkspaceStore` / `RecordingWorkspaceStore` 就在这个文件里,谓词必须与 NAS 逐字同义 | 1, 2, 3 |
| `services/orchestrator/src/orchestrator/tools/file_ops.py` | `list_dir` / `read_file` / 新 `search_files` 工具 | 4, 5 |
| `services/orchestrator/src/orchestrator/tools/workspace_tree.py`(新建) | 树形摘要的纯函数 | 6 |
| `services/orchestrator/src/orchestrator/agent_factory.py` | 系统提示词组装 | 7 |

---

# PR-2b —— 只读文件操作走宿主 NAS

## Task 1:`WorkspaceFileEntry` 加 `mtime`

**Files:**
- Modify: `services/orchestrator/src/orchestrator/tools/workspace_store.py`(约 46 行)
- Modify: `nas_workspace_store.py` 的 `NasWorkspaceStore.list_files`
- Modify: `workspace_store.py` 的 `SupervisorWorkspaceStore` 与 `RecordingWorkspaceStore`
- Test: 三个实现共用的契约测试(自己 grep `WorkspaceFileEntry` 找到它们)

**⚠️ 实现是三个不是两个。** `WorkspaceStore` 协议有 `NasWorkspaceStore`(宿主 NAS)、
`SupervisorWorkspaceStore`(走沙箱 supervisor)、`RecordingWorkspaceStore`(测试替身)。
**三个的谓词必须逐字同义** —— 这个仓库在「SQL 与内存 store 谓词不同义」上栽过不止
一次,而那类 bug 单看任何一个实现的测试都发现不了。

**Interfaces:**
- Produces: `WorkspaceFileEntry(path: str, size: int, mtime: datetime)` —— 后续所有 task 都按这个形状取时间。

- [ ] **Step 1: 写失败的契约测试**

在两个实现共用的契约测试里加:写一个文件、读回 listing、断言 `mtime` 落在写入前后的时间窗内(不要断言精确相等 —— 文件系统时间精度不保证)。

- [ ] **Step 2: 跑,确认红**(`AttributeError` 或 `TypeError`)
- [ ] **Step 3: 加字段并在两个实现里填**

NAS 侧用目录遍历里本来就有的 `stat` 结果,**不要为它多跑一次 `stat`**。时间一律 `datetime`,带 UTC tzinfo。

- [ ] **Step 4: 跑,确认绿;两个实现都要跑到**
- [ ] **Step 5: 变异自证** —— 把 `RecordingWorkspaceStore` 的 `mtime` 写死成固定值,确认契约测试转红(这条是在验「三个实现真的都被测到了」,不只是验字段存在)
- [ ] **Step 6: 提交**

## Task 2:NAS 侧的作用域读

**Files:**
- Modify: `nas_workspace_store.py`
- Modify: `workspace_store.py` 的另外两个实现
- Test: 新建逃逸用例文件

**Interfaces:**
- Produces:`list_files(*, tenant_id, user_id, scope)` 与 `read_file(*, tenant_id, user_id, scope, path)`,`scope` 三档 —— `agent:<agent_key>` / `shared` / `user_root`。

**作用域映射**(从 `workspace_paths.py` 逐条对照来的,不要自己推):

```
EXEC_VIEW + 绑了 agent   →  {user_root}/agents/{agent_key}/
EXEC_VIEW + 没绑         →  {user_root}/
EXEC_VIEW/shared         →  {user_root}/shared/        (只读,写要拒)
```

`agent_key` 是**不可信输入**,必须过 `workspace_paths._require_safe_key` 同款校验:`\A[A-Za-z0-9._-]+\Z`,并且单独拒掉 `.` 与 `..`(两个点都在字符集里,能过正则,而 `{root}/agents/..` 就是 `{root}`)。

- [ ] **Step 1: 先写五类逃逸用例,全部预期抛错**

```python
@pytest.mark.parametrize("bad", [
    "../other-agent/secret.txt",          # 相对穿越
    "/etc/passwd",                        # 绝对路径
    "a/../../../../etc/passwd",           # 深度穿越
    "agents/someone-elses-key/x.txt",     # 借布局保留段跨 agent
    "shared/../agents/other/x.txt",       # 从 shared 绕回去
])
async def test_scoped_read_rejects_escape(bad): ...
```

再加两条 symlink 用例:工作区里放一个指向 `/etc` 的 symlink、一个指向另一个 agent 目录的 symlink,断言**读不到根外的内容**。

- [ ] **Step 2: 跑,确认红**(此时 `scope` 参数还不存在)
- [ ] **Step 3: 实现**

复用 `_open_parent_dir_fd`。作用域只改**起点**(从 `_user_root` 改成 `_user_root/agents/<key>` 等),**不要改走路的方式** —— `openat` 链本身就是防穿越的那道闸。

- [ ] **Step 4: 跑,确认全绿**
- [ ] **Step 5: 变异自证(这条最重要)**

把 `_require_safe_key` 的校验注释掉 → 确认跨 agent 那条用例转红。**如果没红,说明那条用例测的不是它以为的东西,先修用例再继续。**

- [ ] **Step 6: 提交**

## Task 3:`search_files`

**Files:**
- Modify: `nas_workspace_store.py` + `workspace_store.py` 的另外两个实现
- Test: 同 Task 2 的测试文件

**Interfaces:**
- Produces:`search_files(*, tenant_id, user_id, scope, name_glob=None, content=None, max_results=50) -> list[WorkspaceFileEntry]`

契约:

* `name_glob` 与 `content` 至少给一个;两个都给是「文件名匹配**且**内容包含」。
* 内容搜索**只读文本文件**,遇到二进制跳过(不要试图解码)。
* **单文件读取上限**必须有(建议 1 MiB),避免一个大文件把内存吃掉。
* 结果数上限 `max_results`,超了要在返回里带上「被截断」的标记 —— 照 `list_dir` 既有的 `truncated` 约定,不要发明第二种。
* 作用域约束与 Task 2 完全一致(同一个起点解析,不要另写一份)。

- [ ] **Step 1: 写测试** —— 按名字找到、按内容找到、二进制被跳过、超过 `max_results` 时 `truncated=True`、**逃逸用例**(搜索不能越出作用域)
- [ ] **Step 2: 跑,确认红**
- [ ] **Step 3: 实现**
- [ ] **Step 4: 跑,确认绿**
- [ ] **Step 5: 变异自证** —— 去掉二进制跳过,确认对应断言红
- [ ] **Step 6: 提交**

## Task 4:`list_dir` / `read_file` 改走宿主 NAS

**Files:**
- Modify: `services/orchestrator/src/orchestrator/tools/file_ops.py`(`ListDirTool` 约 721 行、`ReadFileTool`)
- Test: `file_ops` 既有测试

- [ ] **Step 1: 改测试** —— 既有的 `list_dir` / `read_file` 测试原来注入的是 `SandboxRuntime` 桩,改成注入 workspace store。**先改测试跑成红**,确认它们真的咬住了实现。
- [ ] **Step 2: 实现**

`resolve_scope` 的返回 `(ws, rel)` 映射成 Task 2 的 `scope`:`ws == EXEC_VIEW` 且有 `agent_key` → `agent:<key>`;`ws == EXEC_VIEW` 无 key → `user_root`;`ws` 以 `/shared` 结尾 → `shared`。

**注释里必须写清 NFS 那条实测结论**(12~15ms,不是 NFSv3 默认推断的 30~60 秒),以及它是怎么测的。

- [ ] **Step 3: 跑,确认绿**
- [ ] **Step 4: 把沙箱读路径的死代码删掉**

`build_list_wrapper` / 读那一侧的 `run_scoped_read` 如果没有别的调用方了就删。**只删你这次改动造成的孤儿**,别顺手清理无关的既有死代码。

- [ ] **Step 5: 全仓 grep 回归** —— `rg 'build_list_wrapper|run_scoped_read'` 确认没有漏掉的调用点(**下游调用点可能还不在你的工作树里,rebase 无冲突不等于能跑**)
- [ ] **Step 6: 提交**

## Task 5:把 `search_files` 挂成工具

**Files:**
- Modify: `file_ops.py`(新增 `SearchFilesTool`)
- Modify: 工具注册处(自己 grep `ListDirTool` 的注册点,跟着它写)
- Test: `file_ops` 测试

- [ ] **Step 1: 写测试** —— 工具层的参数校验 + 结果渲染 + `truncated` 透传
- [ ] **Step 2: 跑,确认红**
- [ ] **Step 3: 实现**

工具描述里要写清它和 `list_dir` 的分工:**先 `list_dir` 看结构,要找具体东西用 `search_files`**。描述是给模型读的,写成人话。

- [ ] **Step 4: 跑,确认绿**
- [ ] **Step 5: 提交**

---

# PR-2 —— 系统提示词里的工作区树形摘要

## Task 6:树形摘要的纯函数

**Files:**
- Create: `services/orchestrator/src/orchestrator/tools/workspace_tree.py`
- Test: `services/orchestrator/tests/test_workspace_tree.py`

**Interfaces:**
- Consumes:`list[WorkspaceFileEntry]`(Task 1 的形状)
- Produces:`render_workspace_tree(entries, *, max_expanded: int = 30) -> str`

**渲染契约**(spec §7 决策二):

```
  style/                    2 files    22.4 KB   最近改 09-16
      PLAN_STYLE.md          1.2 KB    09-16
      render_plan.py        21.2 KB    09-16
  outputs/                147 files     3.1 MB   最近改 09-20
  (根目录)                   3 files    26.1 KB   最近改 09-20
```

* **按目录聚合**,不按文件排序 —— 理由见 spec:按时间排会精确地把该显示的锚挤掉。
* 目录按路径字典序(目录数少,稳定即可)。
* **展开规则**:总展开条数不超过 `max_expanded`;从文件数最少的目录开始展开(小目录信息密度高),装不下的目录退化成一行计数。
* 空工作区返回 `""`,调用方据此整块不出现。

- [ ] **Step 1: 写测试**

至少五条:空输入 → `""`;单目录全展开;大目录退化成计数行;总条数超 `max_expanded` 时哪些被展开(钉住「小目录优先」这条规则);**根目录文件归到 `(根目录)` 这一组**。

- [ ] **Step 2: 跑,确认红**
- [ ] **Step 3: 实现**
- [ ] **Step 4: 跑,确认绿**
- [ ] **Step 5: 变异自证** —— 把「小目录优先」改成「字典序优先」,确认对应断言红
- [ ] **Step 6: 提交**

## Task 7:接进系统提示词

**Files:**
- Modify: `services/orchestrator/src/orchestrator/agent_factory.py`(平台段组装,约 1700-1790 行)
- Test: `services/orchestrator/tests/test_agent_factory_*.py` 里合适的那个

**块的形状**(spec §5):

```
# Workspace (snapshot taken when this run started)
Files already in /workspace. This is a snapshot: it can be stale by the time you
act on it, and it says nothing about content — re-read a file before trusting it.
If a tool fails while looking for something listed here, the failure is the tool's,
not evidence the file is gone.

<树>
```

四条硬要求,每条都有出处:

1. **自声明会过期** —— 照 hermes 的块首措辞。
2. **明说工具失败不等于文件不在** —— 与 PR-1 在错误消息里加的那句存在性免责同一件事的两处落点。
3. **只读元数据,永不读内容** —— 块里只出现路径、大小、时间。
4. **拿不到 listing 时整块不出现**,绝不出现「(无法读取)」这类半截块 —— 半截块会被模型读成「工作区是空的」,那正是本 PR 要治的误判。

- [ ] **Step 1: 写测试** —— 有文件时块出现且含关键行;listing 抛异常时块**不出现**且不影响 agent 构建;空工作区时块不出现
- [ ] **Step 2: 跑,确认红**
- [ ] **Step 3: 实现**

注意 `_compose_system_prompt` 里 B-85 ③ 已经删掉过一次「什么都没有就短路返回 base」——**不要把它加回来**。

listing 失败一律 `logger.warning` + 块不出现,**永不让 run 失败**(照 `inputs_node` 的既定做法)。

- [ ] **Step 4: 跑,确认绿**
- [ ] **Step 5: 变异自证** —— 让 listing 抛异常,确认「agent 仍能构建」那条断言在去掉 try/except 后转红
- [ ] **Step 6: 提交**

---

## 验收(两个 PR 合并后一起做)

**测试环境当场可判,不用等一周:**

1. **`.py` 跨 run 重打 14 → 0** —— 用金丝雀账号跑一轮让它建脚本,**再跑第二轮,看它还写不写**。第二轮仍 `write_file` 同一路径 = 本波主线没生效。
2. **`list_dir` 失败率 11% → ~0** —— 发测试环境后按 60 天口径重算。
3. **⚠️ 必须单独验一条我没测过的**:我的 NFS 一致性实测是 **control-plane pod 到 control-plane pod**,而真实路径是 **沙箱写 → 宿主读**,是另一对客户端。发测试环境后必须真跑一次:沙箱里 `write_file`,紧接着 `list_dir`,确认立刻看得见。**看不见就说明这个设计的前提不成立**,那时再决定是加 `noac` 还是把读路退回沙箱。

## 关联

* PR-1(#1639)—— 另一半:504 不再被读成「不存在」
* B-50 —— 工作区按 agent 分层,Task 2 的作用域映射直接依赖它
* B-60 —— 每次 exec 私有挂载空间;本计划**不碰**它(写路径不动)
