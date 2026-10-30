# B-236 三层工作区:对话目录、开场复制、agent 记忆

日期:2026-10-30 · 状态:已拍板,随 B-236 一起发布

## 这份文档回答什么

- 一个对话里,模型写的文件落在哪、能看到哪些文件。
- 存量 agent 根下的模板 / 风格锚 / 脚本,在新对话里怎么还能按老路径用。
- agent 怎么「记住」跨对话的东西,记在哪,谁能改,改坏了怎么找回。

## 用户拍板(10-30)

| 项 | 决定 |
| --- | --- |
| 过渡期映射(compat:只读 bind + 截止日) | 整个删掉,改成开场复制 |
| 开场复制上限 | 每个对话合计 20 MB;超出的目录整个不复制,告诉模型它在 `/agent/<目录>/` |
| 记忆规矩的写法 | 写**类别**,不写关键词 |
| 版本保留 | 每个文件留最近 20 份 |
| 发布 | 与 B-236 同一次发布 |

## 第一层:每个对话一个 `/workspace`(B-236 原有,不变)

- 对话视图里 `/workspace` = `agents/<key>/threads/<conv>/work/`,`/agent` = agent 根。
- 老对话(发布前开的)仍是老视图:`/workspace` 与 `/agent` 是同一个目录。
- exec 脚本按 fd 挂载、`shared/` 只读、tmpfs 盖住、嵌套 user namespace —— 全部保留。
  删掉的只有过渡期目录那一段(`$4` 个数 + 名字、校验、只读 bind 循环),两份脚本仍逐字相同。

## 第二层:开场复制(取代过渡期映射)

**什么时候。** 对话第一次 run 开始时,图的入口节点 `conversation_seed`(与 `inputs` /
`workspace_ingest` 并列,agent 等它做完)。只在对话视图做;委派子代(`child_run`)不做。

**复制什么。** agent 根下已有的一级**目录**,跳过点开头的名字和保留名(`threads` `uploads`
`inputs` `.tool_results` `skills` `shared` `agents`)。根下的单个文件不复制。本对话 `work/`
里已有同名条目就不覆盖。

**怎么保证只复制一次。**

1. 按对话加锁(Postgres advisory lock,新 classid 8624,键 `{tenant}:{conv}`;不是按用户的
   写锁)。锁外先看一眼标记,锁内再看一次。
2. 标记 `threads/<conv>/.seeded` 在就什么都不做。
3. 每个目录先复制进 `threads/<conv>/.seed-tmp-<随机>/<目录>`,再改名进 `work/<目录>`,
   目标名已存在就不动。
4. 标记最后写。
5. 失败:不写标记、清掉临时目录、记日志(只记异常类型),run 照常;下一次 run 重试,
   已经搬进来的目录按「同名不覆盖」跳过。

**安全。** 宿主侧经 NAS store 自己的防穿越闸拿 agent 根的目录 fd,一路
`openat(O_RDONLY|O_DIRECTORY|O_NOFOLLOW)`;文件 `O_RDONLY|O_NOFOLLOW|O_NONBLOCK` 后 `fstat` 核对
是普通文件;符号链接、FIFO、设备、socket、编码不成 UTF-8 的名字跳过并计数;目标端在钉住的
目录 fd 上 `mkdirat` / `O_CREAT|O_EXCL|O_NOFOLLOW`。保留 mtime。

**上限(依据测试环境 2,079 个 agent 根的实测)。**

| 闸 | 值 | 实测 |
| --- | --- | --- |
| 总字节 | 20 MB(拍板) | p99 0.53 MB,最大 25.5 MB,只有 1 个根超过 |
| 条目数(文件 + 子目录) | 500 | 每根最多 77 个文件,p99 22 |
| 深度 | 12 层 | 最深 5 层 |

先只 `lstat` 量一遍,放不下的目录整个跳过(不读它的字节);复制时再按同一组数守一次。

**模型看到什么。** 工作区快照段里,原样未改的副本在本对话那一节只占一行(「unchanged copy of
/agent/<目录>/」),不占 30 行展开预算;改过的副本照常展开。超出上限没复制的目录在本对话那一节
末尾一行写明 `/agent/<目录>/`(名单存在状态 `seed_skipped_dirs` 里,不对外推送)。

工具描述里的两句话(`WORKSPACE_PATH_RULE` / `EXEC_WORKSPACE_NOTE`)改成:新对话的 `/workspace`
开场带着 agent 各目录的副本,改副本只影响本对话,要留给以后的对话就放 `/agent/`。两种视图里都成立。

## 第三层:agent 自己的记忆

**规矩(系统提示词)。** 只给有沙箱写工具(`write_file` / `edit_file` / `bash` / `exec_python`)
且不是委派子代的构建。写的是类别:

- 存到 `/agent/`:持久的用户偏好(直接说的,或反复纠正同一件事体现出来的)、被纠正过的做法、
  可复用的资产(模板、脚本、报告结构)。
- 每存一样,在索引里记一行「是什么、在哪」。
- 永远不存:客户 / 患者数据或任何能认出具体客户的东西、一次性请求、中间结果。
- 一事一文件,改文件用 `edit_file` 定点改,不整文件重写。
- 改本对话里的副本只影响本对话;要长期生效,改 `/agent/<目录>/…`。
- 这个 agent 的索引是权威;召回的长期记忆与 `MEMORY.md` 只是提示,冲突时以索引为准
  (平台里已有三条记忆通路:`memory_item`、`remember` 草稿、`MEMORY.md` 投影)。

文本不含任何租户的内容。

**索引文件:`/agent/AGENT_NOTES.md`。** 不叫 `MEMORY.md`:那个名字是老视图里长期记忆的投影文件。

**开场注入。** 对话第一次 run 开始时(入口节点 `agent_notes`)读一次索引:

- 门:对话视图、有工作区存储、agent 有写工具、不是子代、是对话的第一轮且还没注入过(重新
  生成第一轮时旧轮被取代,照样注入;同一轮被重跑时不重复)。
- 不可信数据:先过 `scan_for_threats`(strict,与召回记忆同一口径),命中就不载入、只留一行
  `[BLOCKED:<类别>]`;再按构建的 spotlight nonce 包起来(防线开着时)。
- 截到约 2,000 token,截断处写明 `read_file('/agent/AGENT_NOTES.md')`;文件超过 64 KiB 不读,
  只说一句。
- 作为一条隐藏 HumanMessage 落进第一轮(落检查点)。压缩或滑窗把它挤出提示词视图时,放回原件
  (与「本轮输入」段同一做法)。
- **绝不进系统提示词**:构建缓存不分用户。之后对话里的改动经工作区快照段与 `read_file` 看得到,
  不每步重注。

**子代不写索引。** 照 B-122 的边界(子代不永久改变 agent 自己):委派出去的 run 用 `write_file` /
`edit_file` 写 `/agent/AGENT_NOTES.md` 被拒,提示它把要记的写进最终回复。子代的构建也读不到上面的
规矩。已知缺口:子代的 `bash` / `exec_python` 仍然写得到这个文件(沙箱侧没有逐文件的只读),
靠规矩不给子代 + 版本可找回兜底。

**版本(按观察)。**

- 主 run 结束时(`run_agent` 的 `finally`,fire-and-forget;交接给别的副本的不算结束)扫一遍
  `/agent`:一级的非保留文件与目录,含索引;不进保留区与点开头的目录;深度 / 条目数沿用开场复制
  的数,一次最多读 20 MiB,单个文件超过 8 MiB 不留。只看对话视图的主 run。
- 先比大小 + mtime,变了再算 sha256;内容没变只对齐副本的 mtime。
- 存在 `{root}/{tenant}/.agent-file-versions/{user}/{agent_key}/{sha256(路径)[:32]}/`,用户根
  旁边,沙箱挂不到;每个文件留最近 20 份。
- 失败只记日志与计数,不影响 run。
- 清除用户(`purge_user`)与已删工作区到期销账(留存作业)都删这个用户的整个版本目录。

## 删掉了什么

`workspace_compat.py`、`EXPERT_WORK_WORKSPACE_COMPAT_ROOT_DIRS_UNTIL`(测试 / 生产 overlay)、
解析器里的 `compat` 一档与拒写提示、`require_compat_dir_name`、`WorkspaceView.compat_dirs`、
`ToolContext.workspace_compat_dirs`、命中计数、快照段里的过渡期那一行、exec 脚本与 supervisor /
runner 里的过渡期参数,以及相关测试。

## 没有做 / 要留意

- 不需要迁移(全在 NAS 与检查点状态里)。
- 新镜像:runner 脚本变了(去掉过渡期参数),随 B-236 的镜像一起发。
