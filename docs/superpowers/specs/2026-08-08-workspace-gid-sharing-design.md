# 工作区跨 uid 权限:gid 共享设计(W2-BUG-1)

> 修的是沙箱迁移波 2 端到端验收(2026-08-08)逮到的一条 Important——
> agent 用标准文件工具写出来的文件,用户在前端下载不了。
> 上游背景见 `docs/research/2026-08-07-sandbox-w2-probe-results.md` § 十、
> 波 2 设计见 `docs/superpowers/specs/2026-08-07-sandbox-migration-w2-design.md`。

## 一、问题

### 症状

前端工作区页面列得出 `MEMORY.md`(781 字节),点下载报 404「file not found」。
文件就在那儿。

### 根因链

```
PermissionError: [Errno 13] Permission denied: 'MEMORY.md'
  nas_workspace_store.py:467  os.open(name, O_RDONLY | O_NOFOLLOW, dir_fd=dfd)
→ SandboxSupervisorError("workspace file not found")
→ HTTP 404
```

NAS 上 `MEMORY.md` 是 `-rw------- 1 10000 10000`。写它的是沙箱里的 agent
(uid 10000);读它的是 control-plane(uid 10002)。**两个 uid 之间没有任何
共享凭据**——不同 uid、不同 gid、`0600` 连 group 位都没有。POSIX 直接拒绝。

`0600` 不是 umask 造成的,是**代码结构决定的**。`file_ops.py:141`:

```python
def _atomic_write(full, data):
    parent = os.path.dirname(full) or _WS
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=parent)     # ← mkstemp 恒定 0600,与 umask 无关
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, full)                  # ← 0600 被原样搬到目标文件名
    ...
```

`tempfile.mkstemp` 硬编码 `0600` 是它的安全契约(临时文件不该对别人可见),
`os.replace` 保留源 inode 的权限位。所以**每一个经这条路写出来的文件都是
`0600`**,和进程 umask 无关。

### 范围比"一个 MEMORY.md"大得多

同一个用户目录下 `out.txt` 是 `-rw-rw-rw-`,下载正常。差别只在写它的代码路径:

| 写入路径 | 落地 mode | control-plane 能读 |
|---|---|---|
| `exec_python` 里裸 `open(..., "w")` | `0666 & ~umask` = `0666` | ✅ |
| `write_file` / `edit_file` / 状态投影(`PLAN.md`/`TODO.md`/`MEMORY.md`) | **恒 `0600`** | ❌ |

也就是说:**agent 用平台提供的标准文件工具写的每一个产物,用户都下载不了;
只有 agent 绕开工具、在 `exec_python` 里手写文件才能下载。** 这个行为对用户
完全不可预测——同一个目录里两个文件,一个能下一个不能,原因藏在实现细节里。

### 为什么波 2 之前不存在

波 2 之前工作区在沙箱本地卷,control-plane 通过 supervisor 的 HTTP 边界读文件
——读操作发生在 supervisor 容器内部,与写方同 uid。权威搬到 NAS、control-plane
改成直接 POSIX 读之后,"两个进程以不同 uid 访问同一棵树"才第一次成为读路径上
的真实约束。

现有的 `_chmod_workspace_mount` / `_ensure_workspace_dir` 只放**目录**权限
(`0o777`),管不到目录里后续新建的文件——目录再宽,`0600` 的文件也读不了。

## 二、集群实测的事实(两个探针,2026-08-08)

方案完全押在"NAS 认不认 POSIX 组语义"上,不实测就是赌。两个探针都在测试环境
真 NAS(`nasplugin.csi.alibabacloud.com`,NFSv3)上跑过。

### 探针 1:附加组 + setgid

reader 容器 `runAsUser: 10002` / `runAsGroup: 10002` / Pod
`securityContext.supplementalGroups: [10000]`:

```
=== id ===  uid=10002 gid=10002 groups=10002,10000(agent)
f600  (0600 agent:agent)   → Permission denied     ← 隔离仍在,没被放宽
f640  (0640 agent:agent)   → mode640               ← 附加组被 NFS 认可
shared/f640 (dir 2770)     → inshared
写入 2770 目录              → WROTE
新文件 owner               → 10002:agent           ← setgid 让 group 自动继承
```

结论:**阿里云 NAS 完整支持 AUTH_SYS 附加组与 setgid 目录**。没有
`manage-gids` 之类把客户端组列表忽略掉的行为。

### 探针 2:control-plane 身份能否 chgrp

`agent_sandbox.py:347` 与 `nas_workspace_store.py` 的 docstring 里有一条
"集群实测坐实"的结论撑着现在的 `0o777`:

> **为什么 mkdir+chmod 而不是 mkdir+chown**:`os.chown` 到 10000 直接 `EPERM`

这条结论**没错,但管的是另一件事**。探针 2 把两种操作分开测:

```
=== id === 10002 10002 [10000, 10002]
after mkdir:                uid=10002 gid=10002 mode=0o755
CHGRP(-1, 10000)            ok          ← 属主可把 group 改成自己所属的任一组
CHMOD 2770                  ok
after chgrp+chmod:          uid=10002 gid=10000 mode=0o2770
CHOWN uid 10000             PermissionError  ← 老注释测的是这个,非 root 永远不行
child dir (makedirs):       uid=10002 gid=10000 mode=0o2755
file written by 10002:      uid=10002 gid=10000 mode=0o644
mkstemp raw:                mode=0o600
mkstemp after chmod 0640:   mode=0o640
```

两条关键结论:

1. **改 uid 不行,改 group 行。** POSIX 允许文件属主把 group 改成自己所属的
   任一组;`supplementalGroups: [10000]` 让 10000 成为 control-plane 的所属组
   之一,`os.chown(path, -1, 10000)` 因此合法。老注释的"chown EPERM"说的是
   改 uid,不构成对 chgrp 的否定。
2. **子目录只继承 setgid 位和 group,不继承权限位。** `os.makedirs` 建出来的
   子目录是 `0o2755`——group 只有 `r-x`,**没有 `w`**,沙箱写不进去。所以
   **每个目录都必须显式 `chmod 2770`,不能依赖继承**。这条只有实测才看得见。

## 三、设计

### 权限模型

统一到「control-plane 与沙箱共享 gid 10000」:

| 对象 | 现状 | 目标 | 由谁保证 |
|---|---|---|---|
| `{root}`(`/workspaces`) | `1777` root | 不变 | 运行手册一次性 `chmod`(已做) |
| `{tenant}/` | `0755` uid10002:gid10002 | 不变(不含数据,只是命名空间) | `_openat_dir` |
| `{tenant}/{user}/` | `0777` uid10002:gid10002 | **`2770` uid10002:gid10000** | `_ensure_workspace_dir` + 沙箱侧 `_chmod_workspace_mount` |
| `{user}/` 下的任何子目录(`uploads/` 等) | `0777` / `0755` | **`2770` gid10000** | 建它的那处显式 chmod,不靠继承 |
| `_atomic_write` 写的文件 | **`0600`** | **`0640`** | `_atomic_write` 在 `os.replace` 前 `chmod` |
| `exec_python` 裸 `open()` 写的文件 | `0666` | 不变 | setgid 已让 group=10000,`0666` 的 group 位有 `r` |
| control-plane 上传的文档 | `0644` | **`0640`** | `NasWorkspaceStore.write_file` |
| `{tenant}/.deleted/` + marker | `0700` uid10002 | **不变** | 沙箱不该碰它,共享 gid 反而是倒退 |

setgid 是这套的枢纽:目录带 `s` 位之后,**任何一方**在里面新建的文件/目录,
group 自动是 10000,不需要写入方做任何 `chown`——这很关键,因为 control-plane
(uid 10002)对沙箱写的文件根本没有 chown 权限,反之亦然。

### 为什么这同时是一次安全收紧

现在的 `0o777` 是 world-writable。它之所以被接受,是因为"每个用户目录经
`subPath` 只挂进属于它的那一个沙箱",隔离靠**挂载范围**而不是 POSIX 位。
这个论证成立,但它把两条 CodeQL high 逼成了 `won't fix`
(`agent_sandbox.py:821` 与 `nas_workspace_store.py:522`)。

`2770` 之后,隔离由挂载范围**和** POSIX 位双重保证,`other` 档整个清零。
那两条告警不是被压下去,是真的不存在了。同理 `_openat_dir` 的
`os.fchmod(fd, 0o777)`。

### 错误归因:权限失败不该伪装成"文件不存在"

`NasWorkspaceStore._read` 现在把 `FileNotFoundError` 与 `PermissionError`
一起收成 `SandboxSupervisorError("workspace file not found")`,端点再翻成
404。用户看到「文件不存在」,而它明明列在上一屏——这类问题只能靠翻服务端
日志才诊断得出来。

改法:store 侧把两者分开(权限失败是**服务端配置问题**,不是用户输入问题),
下载端点对权限失败返回 500 而非 404。

**给用户的文案不含路径、uid、mode 等细节**——外部可见的只有"服务端错误",
诊断信息留在结构化日志里。404 继续用于真正不存在 / 跨用户不可见(维持
既有的"404 隐藏存在性"姿态,那条不变)。

### 改动清单

1. **`orchestrator/tools/file_ops.py`** — `_atomic_write` 在 `os.replace`
   之前 `os.chmod(tmp, 0o640)`。这是沙箱内执行的 snippet,**两个后端共用**,
   所以本地 supervisor 后端也跟着变(见下"不做什么")。
2. **`orchestrator/tools/nas_workspace_store.py`** —
   - `_openat_dir` 的 `os.fchmod(fd, 0o777)` → `os.fchmod(fd, 0o2770)` +
     `os.fchown(fd, -1, WORKSPACE_SHARED_GID)`;
   - `write_file` 的 `_LEAF_FILE_MODE` `0o644` → `0o640`;
   - `_read` 区分 `PermissionError` / `FileNotFoundError`;
   - `.deleted` 分支保持 `0o700`,并在注释里点明"这里刻意不共享 gid"。
3. **`orchestrator/tools/agent_sandbox.py`** —
   - `_ensure_workspace_dir`:`os.chmod(path, 0o777)` → `chmod 0o2770` +
     `os.chown(path, -1, WORKSPACE_SHARED_GID)`,并改写那段"为什么 chmod 不
     chown"的 docstring(它现在会误导人:改 uid 不行 ≠ 改 group 不行);
   - `_chmod_workspace_mount`:`chmod 0777` → `chmod 2770` + `chown :10000`
     (它以 `user="root"` 跑,两个都做得到)。
4. **`infra/k8s/base/control-plane/`** — Deployment 加
   `spec.template.spec.securityContext.supplementalGroups: [10000]`。
5. **共享常量** — gid `10000` 出现在 orchestrator 两个模块 + 一份 k8s
   manifest,其真值由**沙箱镜像的 `agent` 用户**决定(`useradd --uid 10000`)。
   在 `expert_work.persistence` 侧(`WORKSPACE_*` 常量已经住在那里)加一个
   `WORKSPACE_SHARED_GID = 10000`,并加一道漂移闸:解析
   `infra/sandbox-image/Dockerfile` 的 `useradd` 行,断言两边一致——照
   `test_image_env_matches_dockerfile` 的既有手法。manifest 里的字面量由
   同一道闸覆盖(解析 YAML 断言 `supplementalGroups == [常量]`)。
6. **契约测试** — `test_sandbox_runtime_contract.py` 补两条,两后端同跑:
   - 经 `write_file` 写出来的文件,mode 的 group 位可读;
   - 用户工作区根目录带 setgid 位。
   这两条正是"契约套件 19/19 全绿却漏掉 BUG-1"的补丁:原套件只验行为
   (写进去读得出),不验**落地权限**,而跨 uid 场景里权限就是行为。

### 存量迁移

测试环境已有 5 个租户子树 + `_scratch`(契约测试产物)+ 两次探针留下的
`_gidprobe` / `_chgrpprobe`。存量文件**多数已经是 group 10000**(属主
`agent:agent`),缺的只是 `g+r` 位与目录的 setgid;control-plane 建的目录/
文件 group 是 10002,需要 chgrp。

一次性 root Job(挂 `workspace-nas`,写进运行手册),而不是代码里的启动自愈:

```sh
find /mnt/workspaces -mindepth 2 -maxdepth 2 -type d ! -name .deleted \
  -exec chgrp -R 10000 {} + -exec chmod -R g+r {} +
find /mnt/workspaces -mindepth 2 -type d ! -path '*/.deleted*' \
  -exec chmod 2770 {} +
rm -rf /mnt/workspaces/_gidprobe /mnt/workspaces/_chgrpprobe
```

**为什么不做代码自愈**:自愈会让"目录权限归谁负责"变成两个答案(建它的那处 +
一个后台修补器),下次出问题时得同时排查两条路径。生产还没上线,这是一次性
成本;写进运行手册反而让"上线前必须做这一步"这件事显式可查。

### 发布顺序(顺序敏感)

`supplementalGroups`(manifest)与 `0640`(代码)**必须同一次 release 落地**:
先上代码后上 manifest,中间窗口里 control-plane 既没有 gid 10000、文件又
不再 world-readable,**所有下载全挂**。同一个 Deployment 更新里 Pod 带新
securityContext + 新镜像一起重建,这一步是原子的。

存量迁移 Job 必须**在发布前**跑完:老目录还是 `0777`/group 10002 时,新代码
写的 `0640` 文件 group 归 10002,control-plane 读得到但沙箱读不到——方向刚好
反过来,同样是坏的。

顺序:① 迁移 Job → ② `release.sh`(manifest + 镜像同时)→ ③ 复验下载。

## 四、不做什么

- **不改 control-plane 镜像的 uid。** 让两侧 uid 相同(都 10000)也能解决
  问题,而且更彻底,但要重建镜像、迁移容器内 `/app` 的属主、影响与工作区
  无关的其它路径。gid 共享用一行 manifest 达到同样效果。
- **本地 supervisor 后端不做特殊处理。** 它的工作区在 docker volume,读写
  双方是同一个容器内的同一个 uid,gid 共享对它没有意义。`_atomic_write` 改的
  是两后端共用的 snippet,本地那边文件跟着变 `0640`,读方同 uid 不受影响。
  契约测试的两条新用例在本地后端上同样成立(group 位可读、setgid 存在),
  所以不需要 backend-specific 分支。
- **不给 `.deleted/` 共享 gid。** 软删 marker 是权威记录,写它的只有
  control-plane;让沙箱侧的 gid 能读写它,正是波 2 终审 Critical-1 要堵的
  那条路。保持 `0700`。
- **不改 404 隐藏存在性的既有姿态。** 只把"权限失败"这一种从 404 里拆出来。

## 五、验收

代码侧:契约套件两条新用例 + 既有 19 条全绿(两后端)。

真栈侧,在测试环境按发布顺序做完之后:

```
□ 迁移 Job 跑完,抽查一个用户目录:drwxrws--- + group 10000
□ agent 用 write_file 写一个文件 → NAS 上是 0640 group 10000
□ 前端下载该文件 → 200,内容逐字节一致(BUG-1 的直接复现用例)
□ 前端下载 MEMORY.md → 200(存量文件,验迁移 Job)
□ agent read_document 读 control-plane 上传的 0640 文档 → 读得到(反方向)
□ agent 在 /workspace 下建子目录再写文件 → control-plane 列得出、下得动
□ 前端删除一个 agent 写的文件 → 成功(目录 group 有 w)
□ 软删闸仍然生效(.deleted 没被这次改动波及)
```

---

## 六、方向变更(2026-08-08):共享 gid → 统一 uid

**上面第三节的 gid 共享方案作废。** 实施到第 3 个 task、Task 3 修到第三轮的时候,
用户提了一个问题:「直接给 root 权限行不行」。答案是不行(见下),但顺着这个问题
重新核对之后发现,我在 § 四「不做什么」里否决「不改 control-plane 镜像的 uid」时,
**给的理由是错的**,而那条恰恰是最简单的解法。

### 两种「给 root」为什么都不选

**沙箱以 root 跑** —— 不解决问题,反而更糟。agent 写的文件属主变成 uid 0,
control-plane(10002)依然不是属主;NFS 一旦开 `root_squash`,root 还会被映射成
`nobody`,连原先能走的路都断了。

**control-plane 以 root 跑** —— 技术上**可行**:波 2 的
`_chmod_workspace_mount` 以 `user="root"` 跑 `chmod` 在真集群上成功过,
这说明本 NAS 的 `root_squash` 是关的,root 读得动任何文件,整套权限代码都能删。
不选的理由不是做不到,是不划算:control-plane 是直接对公网的 HTTP 服务,
带文件上传、路径参数、下载端点——一次路径穿越或 RCE 就直接是 root。
为省一套权限配置把最不该给 root 的进程给成 root,这笔账不划算。

### 选定:两侧统一 uid 10000

不给 root,把 control-plane 镜像的 uid 从 10002 改成 10000,与沙箱镜像的
`agent` 用户一致。两个进程在文件系统看来是同一个人,跨 uid 这件事从根上不存在
——不需要共享组、不需要 setgid、不需要任何 `chown`、不需要装配期闸、不需要
分级日志。

**§ 四否决它时给的理由(「要重建镜像、迁移容器内 `/app` 的属主」)经不起核对**:

```
services/control-plane/Dockerfile:55  RUN useradd --uid 10002 ... expert_work   ← 全仓仅此一处
services/control-plane/Dockerfile:57  COPY --chown=expert_work:expert_work ...  ← 用户名,不是数字
```

所有 `COPY --chown` 用的都是**用户名**,改 uid 后自动跟着;容器里唯一可写的挂载
是 `/mnt/workspaces`(`/app` 只读、`secret-store` 声明了 `readOnly: true`);
全仓没有任何代码或 manifest 拿 `10002` 做逻辑判断,只有注释提到它。改动面是**一行**。

我当时没核对就把它写进了「不做什么」,代价是三个 task 的返工。记在这里,
不是自责,是给下一个读者一个提醒:**「不做什么」那一节里的每条理由,
和正文里的技术判断同等承重,必须同样核对过再写。**

### 新的权限模型

两侧同 uid 之后,权限可以比 gid 方案**更紧**——不再需要给任何"另一方"开口子:

| 对象 | 波 2 现状 | gid 方案(作废) | **统一 uid** |
|---|---|---|---|
| `{tenant}/{user}/` 及子目录 | `0777` | `2770` + gid 10000 | **`0700`**,属主 10000 |
| `_atomic_write` 写的文件 | `0600` | `0640` | **`0600` 不动**——属主就是读方 |
| control-plane 上传的文档 | `0644` | `0640` | **`0600`** |
| `{tenant}/.deleted/` | `0700` | `0700`(刻意不共享) | `0700` 不变 |
| control-plane Deployment | — | `supplementalGroups: [10000]` | **不需要** |

`world-writable` 与 `world-readable` 一起退场,那两条被 dismiss 的 CodeQL high
同样真消失。BUG-1 的直接症状(`_atomic_write` 落 `0600` 读不了)自动没了——
读方就是属主。

### 保留与作废

**保留**(与 gid 无关的独立改进,fix loop 里挣出来的,不该跟着方案一起丢):

- `WorkspacePermissionError` 与权限失败的单独归因(§ 三那一节整段仍然成立)。
- `write_file` 的错误边界必须包住 `with os.fdopen(...)` 的 close/flush ——
  `BufferedWriter` 让小文件的 `ENOSPC` 在 close 才爆,这与 uid 方案无关。
- `list_files` 的 `os.walk(onerror=...)`:不加它,一棵读不动的子树被静默丢掉。
- `user_root.is_dir()` 在 Python 3.14 上会吞掉 `OSError` → 改 `os.stat` +
  `S_ISDIR`(`requires-python = ">=3.12"` 允许 3.14)。
- 建 tenant 子树的 EACCES 要归到 `WorkspacePermissionError` —— 那是运行手册
  第一步的失败,也是本次的旗舰诊断。

**作废**:三个 gid/mode 常量与其 re-export、`supplementalGroups`、
`build_workspace_store` 的装配期闸与 `_process_is_in_shared_gid`、
`_chgrp_denied_level` 分级日志、`_atomic_write` 的 `chmod 0640`、
所有 `chown`/`fchown` 与 setgid。

**改造**:`test_workspace_shared_gid.py` 的三方漂移闸改成钉
**「control-plane 镜像的 uid == 沙箱镜像的 uid」**——两份 Dockerfile 双向比对。
这个闸在新方案下比旧方案更重要:两个数字一旦分叉,症状就是 BUG-1 原样复发,
而两份 Dockerfile 分属不同目录、不同发布线,漂移是迟早的。

`_chmod_workspace_mount` 保留但改语义:平台自动创建 subPath 目录时属主是
`root:root 0755`(集群实测),那种兜底路径下 agent 仍写不进去,所以沙箱侧
以 `user="root"` 跑的兜底改成 `chown 10000:10000`(而不是 `chmod 0777`)。
control-plane 正常先建好目录时属主已经是 10000,平台看到已存在就不会重建,
这条兜底几乎不触发。

### 代价:取证与 uid 分配约定

两条,都接受:

1. **文件属主不再能区分是谁写的。** 现在 NAS 上一眼能看出 `MEMORY.md` 是 agent
   写的、`uploads/w2doc.md` 是 control-plane 写的;统一之后这个信息没了。
   审计仍有 `audit_log`,但文件系统层面的这一层旁证消失。
2. **各服务 uid 分开的现有约定破一个口子**
   (沙箱 10000 / credential-proxy 10001 / control-plane 10002 /
   supervisor 10003 —— 明显是有意分配的)。这里让 control-plane 与沙箱重号。

软删 marker **不受影响**:它在 `{tenant}/.deleted/`,而沙箱的 `subPath` 只挂到
`{tenant}/{user}/`,根本挂不到——靠的是挂载范围的物理隔离,不是 POSIX 位。
波 2 终审 Critical-1 的结论在新方案下同样成立。
