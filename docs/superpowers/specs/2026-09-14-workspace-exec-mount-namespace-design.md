# 写入侧隔离:每次 exec 一个私有的 `/workspace`(B-60)

> 状态:设计已拍板(2026-09-14),待 writing-plans。
> 前置:B-50 用户工作区 agent 维度(spec `2026-09-10-workspace-agent-scoping-design.md`)全部 PR 已合。
> 触发事件:测试环境 2026-09-14 实测,见 §一。

## 一、问题

B-50 之后,一个用户的工作区按 agent 分层:`agents/<agent_key>/`、`shared/`、`uploads/`。
四个文件工具(`read_file` / `write_file` / `edit_file` / `list_dir`)被 `_PRELUDE` 里的
`realpath` + `startswith` 守卫关在 agent 子树内 —— 这是真边界。**`exec_python` / `bash`
不是**:它们在沙箱里跑任意代码,而沙箱里 `/workspace` 就是整个用户根。B-50 spec §5.3
把这点写明为「约定」。

约定在 2026-09-14 被平台自己的主产物路径打穿:

```
exec_python  stdout:  OK /workspace/空白hyq_20260914144350.pptx   ← 写成功,落在用户根
save_artifact 登记:   agents/ai-health-plan-30817804/空白hyq….pptx  ← 登记成功,指向 agent 目录
下载:                 404 —— 两个「成功」说的不是同一个文件
```

模型写 `/workspace/<name>` 不是它犯错:`agent_factory.py:1737` 的提示词明写
「the sandbox working directory is /workspace」,工具描述也以 `/workspace` 为锚。
文件工具会把这个前缀折成 agent 相对路径(`file_ops.py:139-142`),exec 不会。
PR6 摘掉读回落之后,这类文件对 agent 自己的工具和下载端都不可见。

#1551 已把 `save_artifact` 做成强制边界(定位 → 认领用户根上的泄漏文件 → 拒绝),
对接方的流程因此能跑通。但那是产物边界,写入侧仍然是约定。本 spec 把写入侧做成边界。

## 二、目标与非目标

**目标**

1. exec 里的任意代码(含它起的子进程)写 `/workspace/<x>`,落的**就是**
   `agents/<agent_key>/<x>` —— 靠文件系统,不靠约定、不靠提示词。
2. exec 里的代码**看不见**同一用户其他 agent 的目录。
3. 热沙箱按 `(tenant, user)` 复用**不变**;同一沙箱里两个 agent 的 exec **并发**互不影响。
4. 未绑 agent(`agent_key == ""`)与临时沙箱(无用户)的行为一字不改。
5. 两个后端(ACS / 本地 supervisor)可观测结果逐字一致,由契约测试钉住。

**非目标**

- `artifact_version.size_bytes` 回填 —— 另一条。
- `/opt/skills/<agent_key>` 也做成命名空间 —— 同一机制,另一条。
- 每 agent 一个沙箱(B-50 spec §三 否掉的那条)—— 本方案之后不需要。
- 改变 NAS 上的目录布局、DB 里的 `path_in_workspace`、下载端、搬迁脚本 —— 全部不动。

## 三、与 B-50 spec §三 的关系

§三 的结论「挂载点保持用户根」**仍然成立**:CSI 挂载仍是每个沙箱一次、按
`(tenant, user)` 一整个用户根。本 spec 改的不是挂载,是**每个 exec 进程树看到的视图**。
两条硬约束原封不动:`agent_sandbox.py:388-401` 的 `workspace_subpath_prefix` 硬闸不动,
`_workspace_subpath` 仍是 `{tenant}/{user}`。

## 四、机制

### 4.1 Spike 实证(2026-09-14,测试集群池 pod,uid 10000)

| 探针 | 结果 |
|---|---|
| 内核 | Linux 5.10.134(ACS microVM),**不是 gVisor**;`Seccomp: 0`;`unprivileged_userns_clone=1`;`max_user_namespaces=11391` |
| `unshare -Urm` + `mount --bind` | 通 |
| 命名空间外 | bind 目标仍空、源仍在 —— 私有 |
| python 子进程 + 孙进程 | 都看到 bind 后的视图 |
| `mount -o remount,bind,ro` | 写入被拒 |
| tmpfs 盖源目录 | 命名空间内空,外面照旧 |
| 命名空间内写出的文件在外面的属主 | `10000:10000` |
| `unshare -m`(不带 `-U`) | EPERM —— 所以必须走 `-U` |

镜像里 `unshare` / `mount` / `setpriv` 都在(util-linux)。**不需要重建沙箱镜像。**

### 4.2 沙箱内布局

| 路径 | 之前 | 之后 |
|---|---|---|
| NAS 用户根挂载点 | `/workspace` | **`/mnt/workspace`** |
| `/workspace` | = 用户根 | 镜像里的空目录;**只在 exec 的命名空间里**被 bind 成 agent 目录 |
| 文件工具的 `ws` | `/workspace/agents/<key>` | `/mnt/workspace/agents/<key>`(显式真实路径,不依赖命名空间) |
| `shared:` 前缀 | `/workspace/shared` | `/mnt/workspace/shared` |
| `read_document` 的 uploads | `/workspace/uploads` | `/mnt/workspace/uploads` |

常量单源:`sandbox_image_contract.WORKSPACE_ROOT`(`:25`)拆成两个 ——
`NAS_MOUNT = "/mnt/workspace"`(挂载点,建沙箱 / chown / 文件工具用)与
`EXEC_VIEW = "/workspace"`(exec 命名空间里的视图根,命令串与提示词用)。
`workspace_paths.USER_ROOT`(`:30`)改指 `NAS_MOUNT`;`agent_workspace_root()`(`:70`)、
`resolve_scope()`(`:84`)、`file_ops._WORKSPACE_ROOT`(`:74`)、`read_document._WORKSPACE_ROOT`
(`:47`)全部随它走,**不再有第二份 `/workspace` 字面量**。`_require_path` 的折叠
(`file_ops.py:139-142`)改成折 `EXEC_VIEW/` —— 语义正确:模型说的 `/workspace/x` 就是
agent 目录里的 `x`。

### 4.3 exec 命令串(两个后端同一个生成函数)

`agent_sandbox.py:1466-1500` 现在拼的是
`umask 077 && mkdir -p <agent_cwd> && cd <agent_cwd> && python -E -P <script>`。改为由
`orchestrator/tools/exec_view.py` 的 `build_exec_command(agent_key, script)` 生成
(本地后端的 `runner.py` 调同一函数生成子进程命令,见 4.5):

```sh
umask 077 \
 && mkdir -p /mnt/workspace/agents/<key> \
 && unshare -Urm --propagation private -- sh -e -c '
      mount --bind /mnt/workspace/agents/<key> /workspace
      mkdir -p /workspace/shared /workspace/uploads
      [ -d /mnt/workspace/shared  ] && mount --bind -o ro /mnt/workspace/shared  /workspace/shared
      [ -d /mnt/workspace/uploads ] && mount --bind -o ro /mnt/workspace/uploads /workspace/uploads
      mount -t tmpfs -o size=1k none /mnt/workspace
      cd /workspace
      exec python -E -P "$0"' /tmp/ew-exec-<uuid>.py
```

逐条说明:

- `unshare -Urm`:新 user ns(当前 uid 映射为 ns 内 root,这是无特权 mount 的前提)+
  新 mount ns;`--propagation private` 让 ns 内的挂载不外泄(spike 实证)。
- 第一条 bind 之后 `/workspace` 即 agent 目录,读写。
- `shared` / `uploads` 只读 bind 进 agent 目录。**挂载点必须存在**,所以 `mkdir -p`
  —— 它们会以两个空目录留在 NAS 的 `agents/<key>/` 下。浏览面 `list_volume_files`
  只列常规文件,看不见;搬迁脚本与留存 job 不认 `agents/<key>/shared`(它们只看用户根一级),
  不受影响。目录不存在时(临时沙箱、从没上传过)跳过该条,不报错。
- `mount -t tmpfs … /mnt/workspace`:把用户根整个盖掉。**这一条才是「看不见别的 agent」**,
  前面几条只是「写到自己这儿」。size=1k 防止 exec 里的代码拿它当可写盘。
- `exec python "$0"`:脚本路径经 `$0` 传入,不进 shell 字符串拼接。
- 未绑 agent:`agent_key == ""` 时 `mkdir` 跳过、第一条 bind 变成
  `mount --bind /mnt/workspace /workspace`、shared/uploads 两条与 tmpfs 那条**全部省略**
  (它本来就该看到整个用户根)。命令串由同一函数按 `agent_key` 分支,契约测试钉两种形态。
- 临时沙箱(无用户,`/mnt/workspace` 是沙箱本地盘):同一命令串,`agent_key` 按实际取。

`unshare` / 任一 `mount` 失败 → `sh -e` 让整条命令非零退出 → exec 报 `SandboxSupervisorError`
**不回落**。这是 fail-closed:宁可 exec 失败,不让代码落进共享根。

`PYTHONUSERBASE`(`agent_key_envs`,`sandbox.py:96`)不受影响 —— 它指向 `/opt/skills/...`
以外的 `SANDBOX_AGENTS_ROOT`,不在 `/mnt/workspace` 下;`envs` 继续走 `commands.run(envs=)`。

`os.getcwd()` 在 ns 内返回 `/workspace`(bind 挂载点不是符号链接),比今天云后端上的
`/run/csi/mount-root/nas/<hash>` 好看 —— 契约测试 `test_sandbox_runtime_contract.py:76-86`
记录的那个观感问题顺带消失,测试仍按 `(st_dev, st_ino)` 比。

### 4.4 ACS 后端(`agent_sandbox.py`)

1. `_create`(`:1172-1190`):`"mountPath": WORKSPACE_ROOT` → `NAS_MOUNT`。
2. `_chmod_workspace_mount`(`:752`):`chown … WORKSPACE_ROOT` → `NAS_MOUNT`。
3. exec(`:1466-1500`):命令串换成 `build_exec_command(...)`,`cwd=` 仍传 `NAS_MOUNT`
   (进 ns 之前的 cwd 无所谓,但不能是不存在的路径)。
4. `acquire` 时 seed_files 写 `/opt/skills/<key>/…`(`:681-690`),不在挂载下,不动。

### 4.5 本地 supervisor 后端

- `runtime_provider.py:328`:`--volume {vol}:/workspace` → `:/mnt/workspace`;
  `:235-236` `--workdir /workspace` → `/mnt/workspace`(容器起来时 `/workspace` 只是个空目录)。
- `runner.py:86-115`:子进程命令改为 `build_exec_command` 生成的同一条 `unshare …`
  (runner 只负责超时、截断、信封,不再自己 `makedirs`/`cwd`)。
- `docker run` 加 `--security-opt apparmor=unconfined`:ubuntu 上 docker 默认 AppArmor
  策略拒绝容器内 `mount`,seccomp 放行也没用。**这不是放权**:容器仍 `--cap-drop ALL`,
  mount 只在 user ns 内有效。macOS Docker Desktop 无 AppArmor,本地不受影响。
- `infra/sandbox-image/seccomp-profile.json` 已放行 `unshare` / `mount` / `umount2` /
  `setns` / `clone` / `clone3` / `open_tree` / `move_mount`,**不动**。
- runsc:gVisor 实现了 user ns + bind + tmpfs,但本仓库**没实测过**。
  `Acceptance suite under runsc`(`.github/workflows/sandbox-gvisor.yml`)红了就是答案。
  若红:runsc 档降级为「跳过命名空间用例」**不是选项** —— 那等于接受 CI 沙箱与生产沙箱
  行为不同;届时停下来拍板(换 runsc 版本 / 换 CI 运行时)。

### 4.6 热会话的布局版本

旧热会话的 NAS 还挂在 `/workspace`,新命令串在它上面第一条 bind 就会失败(fail-closed,
exec 报错)。处理:

- `sandbox_instance` 加列 `layout TEXT NOT NULL DEFAULT 'user-root'`(迁移 `0155`,
  expand-only)。新建的会话写 `'agent-ns'`。
- `SandboxInstanceStore.claim_warm` 的复用条件加 `layout = 'agent-ns'`;
  拿到 `'user-root'` 会话的调用方走现有销毁重建路径,`destroy_reason = "layout_mismatch"`
  (与 `_WARM_RECONNECT_DESTROY_REASON` / `_WARM_AGE_DESTROY_REASON` 并列,`:194/:200`)。
- 发布时**不需要**手工清池:第一次 acquire 自动换代。

### 4.7 提示词与工具描述

`agent_factory.py:1737` 那句「the sandbox working directory is /workspace」**保留**,
它现在是真的。`list_dir` 等描述里的 `/workspace` 锚不动。**不加**「请写相对路径」之类
的劝导 —— 边界在文件系统上,不靠劝。

### 4.8 拆旧补丁

#1551 在 `save_artifact` 里加的「用户根认领」分支(`file_ops._ARTIFACT_LOCATE_MAIN` 的
`claimed_from_user_root` 路径、`artifact.py` 对应话术与测试)**拆掉**:exec 已经写不到用户根,
留着就是一段带着洞形状的死代码。存在性校验(`not_found` / `not_a_file` /
`path_escapes_workspace` / `forbidden_scope`)**留着**。

## 五、并发与安全性论证

- 命名空间按进程树:同一沙箱里 agent A 与 agent B 的 exec 各在自己的 mount ns,
  A 的 `/workspace` 与 B 的 `/workspace` 是两个不同的 bind。spike 已证外面看不到里面。
- `bash` 持 per-workspace 写锁(`bash.py:55`),`exec_python` 不持 —— 与本方案无关,不改。
- user ns 内的「root」对外仍是 uid 10000:写出的文件属主 10000(spike 实证),
  NAS 上 `0o700`/`umask 077` 语义不变。
- 能不能逃:ns 内能 `umount /mnt/workspace` 露出用户根吗 —— 能,它是自己的 ns。
  所以本方案对**故意**的代码仍不是绝对边界;它保证的是**照平台教法写的代码**不可能越界,
  以及**默认视图**里别的 agent 不存在。与 `/opt/skills/<key>` 今天的强度一致。写进工具描述
  与对外文档时按这个口径说,不说「隔离」。

## 六、测试

| 层 | 用例 |
|---|---|
| 单元(`test_exec_view.py`) | `build_exec_command` 两种形态(绑/未绑)逐字;`$0` 传参不拼接;失败即非零 |
| 单元 | `_require_path` 折叠改为 `EXEC_VIEW/` 后既有用例不变;`agent_workspace_root` / `resolve_scope` 指向 `NAS_MOUNT` |
| 契约(`test_sandbox_runtime_contract.py`,两后端) | exec 内 `realpath("/workspace")` 的 `(st_dev, st_ino)` == `/mnt/workspace/agents/<key>` 在容器外的值;写 `/workspace/x` → NAS `agents/<key>/x`;`os.listdir("/mnt/workspace") == []`;`/workspace/shared` 只读;未绑 agent 看到整个用户根 |
| 契约 | 两个不同 `agent_key` **并发** exec,各自 `ls /workspace` 只见自己的文件 |
| 契约 | 旧布局热会话被 `layout_mismatch` 销毁重建 |
| 验收(本地 docker + runsc) | 同上跑真沙箱 |
| 集成(`test_artifact_tools.py`) | 认领分支删除后,用户根有同名文件时 `save_artifact` 报 `not_found`(不再认领) |
| 真栈(测试环境) | 金丝雀 PASS;探针用户复现 §一:`exec_python` 写 `/workspace/x.pptx` → `save_artifact` → 下载得到,且结果里**没有**「moved into your agent workspace」 |

## 七、发布

1. 合并 → `release.sh test` → 热会话自动换代 → 探针 + 测试人员。
2. 生产按执行单三段 A / B1 / C / B2;B1、B2 钉子重定为含本 spec 的提交。
3. 执行单 `docs/runbooks/2026-09-14-prod-release-checklist.md` 改期,原「今晚 18:00」作废。
