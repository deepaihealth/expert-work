# 工作区可见性设计(B-84 PR-2)

> 状态:**已定稿**(2026-09-20 用户拍板三处决策 + 拍板把只读文件操作一并纳入本波)。
> 起因:B-84 实测 —— 模型看不见自己上一轮写进工作区的文件,于是重打一遍。
> 60 天里这件事发生 14 次,293,012 个字符,约 6,002 秒墙钟。

## 1. 要解决的那一件事

`run 7fad305b`(测试环境,2026-09-16)的时间轴:

```
seq=3    list_dir       → [tool error] sandbox create failed: 504
seq=36   模型           → 「style/ 目录不存在(沙箱曾重建),本次按首次生成建立锚」
seq=22   write_file     → style/render_plan.py   21,173 字符
```

`style/render_plan.py` **一直在 NAS 上**。`/workspace` 挂的是持久卷,不随沙箱生死。
模型重打它,是因为它唯一的探测手段(`list_dir`)失败了,而平台从没主动告诉过它
工作区里有什么。

按「同租户 + 同用户 + 同路径」归组,`.py` 写入里 **14 次是重打,占 `.py` 总字数的
50%**;同一个用户的 `style/render_plan.py` 被重打了 **6 遍**。

PR-1 已经修掉「504 被读成不存在」那一半(错误分类 + 存在性免责)。**本 spec 管
另一半:让模型根本不需要去探。**

## 2. 一处勘误 —— 别照抄 openclaw 的形态

我在调研回报里写过「正解不是列目录,是让那个东西本身常驻上下文」,依据是
openclaw 把 `SOUL.md` / `MEMORY.md` / `BOOTSTRAP.md` 全文注入。

**那个结论套到我们身上是错的。** 两家注入全文,是因为他们注入的是小的身份与记忆
文档(几百到几千字符)。我们要让模型知道存在的东西是**一个 21,173 字符的渲染
脚本** —— 把它全文注进每一轮,正好是我们要消灭的那笔开销本身。

所以本设计取**清单**,不取全文:

| | 注入全文 | 注入清单 |
|---|---|---|
| 适配的东西 | 小的身份 / 记忆文档 | 任意大小的产物与脚本 |
| 回答的问题 | 「我是谁、我记得什么」 | 「这东西在不在、多大、什么时候改的」 |
| 我们的实测需求 | ✗ | ✓ |

hermes 的 `build_coding_workspace_block` 同样不是全文,是**事实块**(分支、脏文件
计数、verify 命令)。两家都**不给目录清单**,是因为两家的 agent 都跑在本地文件系统
上、`search_files` 随时可用且从不失败;我们的工作区在 NAS 上、要穿过一个会 504 的
沙箱才够得着。**同样的问题在两家那里不成立,所以不能指望抄到现成答案。**

## 3. 地基:不需要起沙箱

`services/control-plane` 的 Pod **自己挂着 NAS**:

```yaml
# infra/k8s/base/control-plane/deployment.yaml:50
- name: workspace-nas
  mountPath: /mnt/workspaces
```

并且 `NasWorkspaceStore.list_files(tenant_id=..., user_id=...) -> list[WorkspaceFileEntry]`
**已经存在**(`services/orchestrator/src/orchestrator/tools/workspace_store.py:46`,
`WorkspaceFileEntry = {path, size}`)。

**所以列工作区是一次本地目录遍历,零 sandbox acquire、零 exec、零新 I/O 代码。**
这条决定了本设计可以做成「每个 run 无条件做」,而不是「按需、可选、怕贵」。

⚠️ `WorkspaceFileEntry` 目前**只有 `path` 和 `size`,没有 mtime**。§7 决策三要定要不要加。

## 4. 放在哪一层

放**系统提示词**,在 `agent_factory` 的平台段里,与 `# Available skills` 同层。

理由:

* 系统提示词**每个 run 组装一次**,不是每轮。run 之内天然字节稳定 —— 这正是
  hermes 给工作区快照做的事(注释原文 `cache safety`:探一次、之后原样重放)。
* `workspace_ingest` 节点是 graph 内的、跑在系统提示词之后,拿它注入就变成一条
  额外消息,拿不到前缀缓存的好处。
* B-67 的 `inputs_node` 是另一条路(run-start 写文件进沙箱),那条适合「要落到
  磁盘给脚本读」的东西;清单是给模型读的,不该落盘。

## 5. 块的形状

```
# Workspace (snapshot taken when this run started)
Files already in /workspace. This is a snapshot: it can be stale by the time you
act on it, and it says nothing about content — re-read a file before trusting it.
If a tool fails while looking for something listed here, the failure is the tool's,
not evidence the file is gone.

  style/PLAN_STYLE.md              1.2 KB
  style/render_plan.py            21.2 KB
  张女士_20260916141342.json        8.7 KB
  ... (还有 N 个文件未列出,合计 M KB)
```

四条设计约束,每条都有出处:

1. **自声明会过期。** hermes 的块首原文是
   `Workspace (snapshot at session start — re-check with git before acting on it)`。
   我们已经在别处栽过「勘察快照会过期」这一跤(班车 1 实录)。
2. **明说工具失败不等于文件不在。** 与 PR-1 的存在性免责同一句话的两处落点 ——
   模型在提示词里和在错误消息里都该读到。
3. **超预算要降级,并且明说自己降级了。** openclaw 预留 150 字符
   (`COMPACT_WARNING_OVERHEAD`)给
   `⚠️ Skills truncated: included N of M ...`,并且降级后仍保留路径让模型自己读回来。
   照抄这个做法:截断时块尾写明「还有 N 个未列出」,而不是静悄悄少几行。
4. **只读元数据,永不读内容。** 块里只出现路径与大小。工作区里可能有客户数据,
   把内容搬进系统提示词既贵又是个数据面问题。

## 6. 不做的

* **不改 `list_dir` 的语义**。清单是补充,不是替代 —— 模型仍然要能在一轮之内看到
  自己刚写的东西,那是 `list_dir` 的活。
  **⚠️ 勘误(09-20 加 §7b 之后)**:这一条原本写的是「不改 `list_dir` 工具」,而
  §7b 恰恰要把它的**实现**从沙箱挪到宿主 NAS —— 两句直接打架。改的是实现与失败率,
  **对模型可见的语义(参数、返回形状、作用域)一个字不变**,这才是这一条真正要守
  的东西。
* **不做实时**。块是 run 起点的快照,run 之内不刷新(刷新就毁掉前缀缓存,而这
  是本波唯一一处会碰缓存的改动)。
* **不注入文件内容**。见 §2。
* **不按 agent 过滤内容的正确性做兜底**:跨 agent 的隔离已经由 B-50 的工作区分层
  负责,本块只是照着现有分层把结果读出来。

## 7. 三处决策(2026-09-20 用户拍板)

### 决策一:只列当前 agent 自己的目录 ✅

B-50 之后工作区按 agent 分层(`{NAS_MOUNT}/agents/{agent_key}/`)。本块只读当前
agent 那一层,不列同一用户其它 agent 的产出。本 spec 要回答的是「这个 agent 上次
建的锚还在不在」,别的 agent 的文件既不相关也会把块撑大。

### 决策二:按目录摘要,不按文件排序 ✅

**我最初给的两个选项(最近修改优先 / 字典序)在这个场景下都是错的。** 看真实
工作区的构成就清楚了:

| 文件 | 性质 | 模型需要知道吗 |
|---|---|---|
| `style/render_plan.py` | 跨 run 复用的锚,**很旧** | ✅ 正是要防重打的那个 |
| `style/PLAN_STYLE.md` | 同上 | ✅ |
| `张女士_20260916141342.json` | 单客户产出,**最新** | ❌ 永不复用 |

**「最近修改优先」会精确地把该显示的挤掉** —— 锚是旧的,新的全是一次性产出。
字典序同样不行:几百个以客户名开头的 JSON 会把 `style/` 冲出预算。

**两个开源项目没有现成答案可抄。** aider 的 repo-map 用 PageRank 排序,但那是对
**代码依赖图**跑的 —— 工作区不是代码库,没有符号引用可以建图;hermes 那边还挂着
一个 issue(NousResearch/hermes-agent#535)在求这个能力,说明他们也没有。所以这
一节是设计,不是移植,相应地要更谨慎。

**取法:按目录摘要。**

```
  style/                    2 files    22.4 KB   最近改 09-16
      PLAN_STYLE.md          1.2 KB    09-16
      render_plan.py        21.2 KB    09-16
  outputs/                147 files     3.1 MB   最近改 09-20
  (根目录)                   3 files    26.1 KB   最近改 09-20
```

为什么这个对:

* **上限由目录数决定,不由文件数决定。** 工作区再大,目录就那么几个 —— 天然有界,
  不需要「60 条还是 2,000 字符」这种拍脑袋的阈值。
* **小目录全展开,大目录只给计数。** 具体阈值(每个目录展开几个文件)是实现细节,
  定在计划里;原则是「展开的总条数有硬上限,超了的目录退化成一行计数」。
* **不需要猜相关性**,而按时间或按字典序排都是在猜。
* 与业界通用原则一致:**用指针 + 摘要替代截断**,别把信息直接毁掉。

### 决策三:给 `WorkspaceFileEntry` 加 mtime ✅

`WorkspaceFileEntry` 现在是 `{path, size}`。加 `mtime`:

* 它正是决定「复用还是重建」的那个字段 —— 模型要能分辨「这是我上一轮写的」和
  「很久以前的」。
* `stat` 本来就在目录遍历里,运行成本近似为零。
* 代价是动一个跨实现的 dataclass:内存实现与 NAS 实现都要跟着改,它们的契约测试
  也是。**两边的谓词必须逐字同义** —— 这个仓库在「SQL 与内存 store 谓词不同义」
  上栽过不止一次。

## 7b. 只读文件操作改走宿主 NAS(用户 2026-09-20 拍板纳入本波,独立 PR)

### 为什么这条比提示词块更值钱

`list_dir` 今天走的是 `run_scoped_read(self.client: SandboxRuntime)` —— **每一次列
目录都是一次沙箱 exec**。实测 60 天失败率 **11%(22/207)**,而 `7fad305b` 那次重打
21,173 字符的直接触发点就是一次 `list_dir` 的 504。

提示词块治的是「模型不知道有什么」;这一条治的是「模型想查也查不到」。两条都要。

### 实测:NAS 跨客户端是即时一致的

我原本担心 NFS 属性缓存(NFSv3 默认目录属性缓存 30~60 秒)会让宿主侧读到过期
目录。**实测推翻了这个担心。**

测法:两个 control-plane pod 挂的是同一个 NAS,当作两个客户端。B 先连续 `ls` 五秒
把目录缓存焐热,A 再创建文件,B 立刻轮询。

```
第 1 轮:  12 ms 后可见
第 2 轮:  13 ms 后可见
第 3 轮:  13 ms 后可见
内容改动: 15 ms 后读到新值
```

挂载参数(测试集群实测):

```
vers=3, rsize=1048576, wsize=1048576, hard, nolock, noresvport,
proto=tcp, timeo=600, retrans=2, sec=sys, local_lock=all
```

没有 `noac` 也没有显式 `actimeo`,但实测行为就是即时的。**结论以实测为准,不以
NFS 默认值的推断为准** —— 这一条要写进代码注释,否则下一个人会照着理论把它改回去。

### 地基

* `services/control-plane` 的 Pod 挂着 NAS(`infra/k8s/base/control-plane/deployment.yaml:50`,
  `mountPath: /mnt/workspaces`)。
* **orchestrator 没有独立 deployment**,它就跑在 control-plane 进程里 —— 所以工具
  代码天然够得着这个挂载点。
* `NasWorkspaceStore` 已有 `read_file` / `list_files` / `write_file` / `delete_file`,
  并且已经带边界安全的路径处理(`_open_parent_dir_fd` 那一套)。

### 范围

改 `list_dir` / `read_file` 走宿主 NAS,并新增 `search_files`(按文件名与内容找)。

**写操作一律不动,仍走沙箱。** 理由:`write_file` / `edit_file` 的语义与 B-60 的
私有 `/w` 挂载空间、以及工作区写锁绑在一起,挪它是另一个量级的改动,而本波的收益
全部来自读路径。

### ⛔ 安全约束(这条是本 PR 的主要风险)

沙箱里的路径语义带两层:B-50 的 agent 分层绑定、B-60 的每次 exec 私有挂载空间。
挪到宿主侧意味着**在 `NasWorkspaceStore` 上重做一遍作用域解析**,而那是路径逃逸的
汇点。

硬要求:

1. **不要新造路径解析。** 复用 `NasWorkspaceStore` 既有的边界安全原语;`resolve_scope`
   的语义要逐条对照,不是照着感觉重写一遍。
2. **判据比 `/workspace` 字面量。** 我们在 cwd 比较上同一个文件里错过三次 ——
   `getcwd(2)` 返回的是 realpath,而挂载点是 symlink。用 `(st_dev, st_ino)` 比,不要
   比路径字符串。
3. **symlink 不 resolve 出根。** 留存链那一波已经定过这条规矩,这里同样适用。
4. **必须有逃逸用例**:`../` 穿越、绝对路径、指向根外的 symlink、其它 agent 的
   目录、其它租户的目录 —— 每一种都要有一条红得起来的测试。

### 与提示词块的关系

两者共用同一套宿主侧读路径,所以**必须同一波做**:分两波等于那套路径写两遍,而
它是安全面,写两遍就是两份要审的逃逸风险。

## 8. 验收

本 PR 在 B-84 本波的生效指标是**第 4 条**:

```
`.py` 跨 run 重打次数    14 → 0        (提示词块)
list_dir 失败率          11% → ~0      (宿主 NAS 读路径)
```

测试环境当场可判,不必等一周:**用金丝雀账号跑一轮让它建脚本,再跑第二轮,看它
还写不写。** 第二轮若仍然 `write_file` 同一路径,本 PR 就没生效。

## 9. 关联

* PR-1(#1639)—— 另一半:504 不再被读成「不存在」
* B-50 —— 工作区按 agent 分层,本 spec 的决策一直接依赖它
* B-67 —— `inputs_node` 是 run-start 注入的先例(那条走沙箱落盘,本条不落盘)
