# 写入侧隔离:每次 exec 一个私有的 `/workspace`(B-60)

> 状态:设计已拍板(2026-09-14 下午);**同日晚 writing-plans 期间按实证修订 9 处(§零),待复审**。
> 前置:B-50 用户工作区 agent 维度(spec `2026-09-10-workspace-agent-scoping-design.md`)全部 PR 已合。
> 触发事件:测试环境 2026-09-14 实测,见 §一。
> 实施计划:`docs/superpowers/plans/2026-09-14-workspace-exec-mount-namespace.md`。

## 零、修订记录(2026-09-14 晚,出计划时逐个落点核对的结果)

下面每条都是「拍板稿写错了 / 没写」,不是设计方向变化。方向不变:挂载仍按用户,每次 exec 一个私有视图。

| # | 拍板稿怎么写 | 实证 | 现在怎么写 |
|---|---|---|---|
| 1 | 文件工具的 `ws` 用真实路径 `/mnt/workspace/agents/<key>`,「不依赖命名空间」 | 文件工具的片段**也是经 `exec` 跑的**(`run_in_sandbox` → `client.exec`),同样进命名空间;进去之后 `/mnt/workspace` 已被 tmpfs 盖住,那条路径不存在 | **沙箱内所有代码看同一个视图**:文件工具的 `ws` = `/workspace`,`shared:` = `/workspace/shared`。§4.2 |
| 2 | `uploads/` 从用户根只读挂到 `/workspace/uploads` | `uploads/` 自 B-50 PR4 起就在 **`agents/<key>/uploads/`**(`uploads.py:247` `workspace_agent_path(..., agent_key=)`),不在用户根;挂上去会把 agent 自己的上传目录遮掉 | **不挂 `uploads`**。只挂 `shared`。§4.3 |
| 3 | 「不需要重建沙箱镜像」,默认 `/workspace` 在镜像里 | 池 pod 实测:镜像里**没有** `/workspace`(W2 Task 9 删的),rootfs 是 overlay `rw` | 镜像仍不动;ACS 建沙箱后以 root `mkdir -p /workspace`(post-create,失败即建沙箱失败);本地 docker 加 `--tmpfs /workspace:ro,size=4k` 让 docker 造出挂载点。§4.4 / §4.5 |
| 4 | 「seccomp 已放行 unshare/mount/…,不动」 | 那条规则是 **`includes.caps=[CAP_SYS_ADMIN]`** 门控的,`clone` 也把 `CLONE_NEWUSER|NEWNS` mask 掉;docker **默认** profile 同样挡。本地 docker 实测 `unshare: Operation not permitted` | 加两条无 cap 门控的规则;本地后端从此**必须**配钉住的 profile(`None` 不再是「宿主默认」)。§4.5,实证表 §4.1 |
| 5 | (未提) | 镜像 util-linux 2.41.5 走**新挂载 API**(`open_tree/move_mount/fsopen…`);profile 默认 `SCMP_ACT_ERRNO` 回 EPERM 不是 ENOSYS,libmount 不回落经典 `mount(2)` → `mount --bind` 报 permission denied | 七个新 API syscall 一并放行(无 cap 门控)。§4.5 |
| 6 | `--security-opt apparmor=unconfined` 一句话 | `docker-default` AppArmor profile 含 `deny mount,`;Ubuntu 24.04 又有 `kernel.apparmor_restrict_unprivileged_userns=1`,unconfined 进程建不了 userns。**未实测**(本机 Docker Desktop 无 AppArmor) | 本地后端加 `apparmor=unconfined`;CI 两个工作流加 sysctl 步骤;由 CI 集成 job 验证,红了按 §4.5 的分支处理 |
| 7 | supervisor 线协议沿用 `cwd` 字段 | 它不再是 cwd(子进程 cwd 恒为 `/workspace`),它是「bind 到 `/workspace` 上的那个目录」 | 改名 `agent_root`。§4.5 |
| 8 | (未提) | 视图里 `/workspace/shared` 是只读 bind;绑了 agent 的调用写相对路径 `shared/x` 会撞 EROFS | `shared/` 与 `agents/` 一样成为绑了 agent 时的**保留首段**,`_require_path` 直接拒并指向 `shared:` 前缀。§4.2 |
| 9 | `claim_warm` 复用条件加 `layout='agent-ns'` | 常量写死在 store 里,列与闸就没法先于 exec 改动单独上线(闸会把好好的 `user-root` 会话全重建成名不副实的 `agent-ns`) | `layout` 由调用方传入,`claim_warm` 把赢家行的 layout 随同一次 SELECT 返回;先落列+闸(传 `user-root`,零行为变化),再随 exec 改动翻成 `agent-ns`。§4.6 |
| 10 | compose 用 `${PWD}` 把 profile 挂到宿主同路径 | docker CLI 是**客户端**读这个文件、把 JSON 内联进发给 daemon 的 HostConfig(实测 2026-09-14:`docker run --security-opt seccomp=/nonexistent/x.json` 客户端直接报错;`docker inspect` 能看到 `seccomp={…json…}`),路径只需要在 **supervisor 容器里**能读到,不是宿主路径 | 实际形状(Task 8):volume `./sandbox-image/seccomp-profile.json:/etc/expert-work/seccomp-profile.json:ro`,env `EXPERT_WORK_SANDBOX_SECCOMP_PROFILE_PATH=/etc/expert-work/seccomp-profile.json`。§4.5 |
| 11 | (未提部署滚动窗口) | control-plane 生产跑 2 副本、默认 RollingUpdate;PR-C 翻转 `layout` 默认值那一刻起,滚动窗口(约 1-2 分钟)内新旧 pod 对同一批热会话的期望布局不一致,双方都可能把对方刚建的热会话判成 `layout_mismatch` 销毁重建,可能连带打断另一侧正在跑的 exec | **拍板(2026-09-14,「不改部署形状」)**:接受为有界抖动,不改 Recreate / 不缩容到零 / 不改发布策略;写进 §7 与 Task 13 验收要点(滚动窗口内预期看到 `layout_mismatch` 销毁,只剩一个版本后自愈)。 |

## 一、问题

B-50 之后,一个用户的工作区按 agent 分层:`agents/<agent_key>/`、`shared/`。
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
- 改变 NAS 上的目录布局、DB 里的 `path_in_workspace`、下载端、搬迁脚本、留存 job —— 全部不动。

## 三、与 B-50 spec §三 的关系

§三 的结论「挂载点保持用户根」**仍然成立**:CSI 挂载仍是每个沙箱一次、按
`(tenant, user)` 一整个用户根。本 spec 改的不是挂载,是**每个 exec 进程树看到的视图**。
两条硬约束原封不动:`agent_sandbox.py:388-401` 的 `workspace_subpath_prefix` 硬闸不动,
`_workspace_subpath` 仍是 `{tenant}/{user}`。

## 四、机制

### 4.1 实证

**测试集群池 pod(2026-09-14 下午,uid 10000,无 seccomp)**

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
| 镜像里 | `unshare`/`mount`/`setpriv` 在(util-linux 2.41.5);`sh` = dash;**没有 `/workspace`**;`/mnt` 在(root 0755);rootfs overlay **rw** |

**本地 docker(2026-09-14 晚,`expert-work-sandbox:dev`,后端同款加固参数:`--read-only --user 10000:10000 --cap-drop ALL --security-opt no-new-privileges --security-opt seccomp=<profile> --pids-limit 128`)**

| 探针 | 结果 |
|---|---|
| 仓内 `seccomp-profile.json` 原样 | `unshare: Operation not permitted`(规则带 `CAP_SYS_ADMIN` 门控) |
| docker **默认** profile | 同样 EPERM —— 「dev 走宿主默认」这条路对 B-60 走不通 |
| profile + `unshare`(mask 到 `NEWUSER\|NEWNS`)+ `mount` 两条无 cap 规则 | userns 通;`mount --bind` 报 **permission denied**(新挂载 API 被 EPERM) |
| 再放行 `open_tree/move_mount/fsopen/fsconfig/fsmount/fspick/mount_setattr` | **整套通**:绑定视图只见自己 + `shared`(只读,EROFS 30)/`/mnt/workspace` 空 / 子进程同视图 / 未绑看到整个用户根 / 外面 `wrote.txt` 属主 `10000:10000` mode 600 / 坏参数 exit 1 |
| `--tmpfs /workspace:ro,size=4k` 当 bind 目标 | 通(docker 在只读 rootfs 上造出挂载点) |
| `--security-opt apparmor=unconfined`(Docker Desktop 无 AppArmor) | 被接受、无副作用 |

### 4.2 沙箱内布局

| 路径 | 之前 | 之后 |
|---|---|---|
| NAS 用户根挂载点 | `/workspace` | **`/mnt/workspace`** |
| `/workspace` | = 用户根 | 空目录(ACS:建沙箱后 root `mkdir -p`;本地:docker `--tmpfs :ro`);**只在 exec 的命名空间里**被 bind 成 agent 目录 |
| 文件工具的 `ws` | `/workspace/agents/<key>` | **`/workspace`**(视图根;它们的片段与用户代码在同一个命名空间里) |
| `shared:` 前缀 | `/workspace/shared` | `/workspace/shared`(视图里的只读 bind) |
| 未绑 agent | `/workspace` = 用户根 | `/workspace` = 用户根(bind 整个 `/mnt/workspace`,不盖、不挂 shared) |

常量单源:`sandbox_image_contract.WORKSPACE_ROOT`(`:25`)**删除**,拆成两个 ——
`NAS_MOUNT = "/mnt/workspace"`(挂载点:建沙箱 metadata.mountPath / chown / 本地 `--volume`、`--workdir`;
**沙箱内代码永远不该出现这个字符串**)与 `EXEC_VIEW = "/workspace"`(视图根:命令串、提示词、
工具描述、文件工具的 `ws`)。`workspace_paths.USER_ROOT`(`:30`)**删除**;`agent_workspace_root()` 删除,
换成两个语义清楚的函数:

- `agent_nas_root(agent_key)` → `/mnt/workspace/agents/<key>`,**只给两个后端拼 exec 用**(bind 源);空 key 拒绝;
- `agent_view_alias(agent_key)` → `/workspace/agents/<key>`,**只用于折叠**模型照旧写的绝对拼法,不指向任何真实目录。

`resolve_scope` 返回 `(EXEC_VIEW, rel)` 或 `(EXEC_VIEW/shared, rel)`。`_require_path` 折 `agent_view_alias/`、再折 `EXEC_VIEW/`;
绑了 agent 时**保留首段**从 `agents` 扩成 `agents` + `shared`(裸 `shared/x` 在视图里是只读 bind,写会 EROFS;拒掉并指向 `shared:` 前缀)。
`save_artifact._validate_path` 同规则。本地 supervisor 的 runtime 包(`runtime_provider.py`)不能 import orchestrator,
自带一份 `SANDBOX_NAS_MOUNT` / `SANDBOX_EXEC_VIEW`,由契约文件末尾的漂移闸钉住相等。

### 4.3 exec 命令串(两个后端同一段 sh)

脚本体是一个**字面量**,参数走位置参数,不做字符串拼接。`orchestrator/tools/exec_view.py`:

```sh
set -eu
root="$1"; shift
if [ -n "$root" ]; then
  mkdir -p "$root"
  mount --bind "$root" /workspace
  if [ -d /mnt/workspace/shared ]; then
    mkdir -p /workspace/shared
    mount --bind /mnt/workspace/shared /workspace/shared
    mount -o remount,bind,ro,nosuid,nodev,noexec /workspace/shared
  fi
  mount -t tmpfs -o size=1k none /mnt/workspace
else
  mount --bind /mnt/workspace /workspace
fi
cd /workspace
exec "$@"
```

调用形态(`EXEC_VIEW_ARGV_PREFIX` + `$1` + python argv):

```
unshare -Urm --propagation private -- sh -c '<脚本体>' ew-exec-view <agent_nas_root 或空串> <python argv…>
```

- ACS:`umask 077 && ` + `shlex.join(...)`,python argv = `python -E -P /tmp/ew-exec-<uuid>.py`,经 `commands.run(cwd=NAS_MOUNT)`。
- 本地 runner:`subprocess.run([...同一前缀..., agent_root or "", sys.executable, "-E", "-P", "-c", code])`,不再自己 `makedirs`/传 `cwd`。
- `mkdir -p "$root"` 在命名空间**内**做(ns 里的 root 映射到外面的 10000,建出来的目录属主是 10000,umask 077 继承);两后端因此共用同一段。
- `remount,bind,ro,nosuid,nodev,noexec`:userns 里 remount 不能**去掉**源挂载上锁住的 flag,但可以**加**;三个都写上永远合法。
- `mount -t tmpfs … /mnt/workspace`:把用户根整个盖掉。**这一条才是「看不见别的 agent」**。`size=1k` 防止拿它当可写盘。
- `shared` 的挂载点会以空目录留在 NAS 的 `agents/<key>/shared/`。浏览面 `list_volume_files` 只列常规文件看不见;搬迁脚本与留存 job 不认 `agents/<key>/shared`。可接受。
- 未绑 agent(`$1` 为空):只 bind 整个 `/mnt/workspace`,不挂 shared、不盖。
- 任一条失败 → `set -e` 非零退出 → exec 报 `SandboxSupervisorError` **不回落**。fail-closed。
- `$0` = `ew-exec-view`,只是报错时的名字。
- `PYTHONUSERBASE`(`agent_key_envs`,`sandbox.py:96`)不受影响 —— `/opt/agents/...` 不在 `/mnt/workspace` 下。
- `os.getcwd()` 在 ns 内返回 `/workspace`(bind 挂载点不是符号链接)—— 契约测试 docstring「其四」记的观感问题顺带消失。

### 4.4 ACS 后端(`agent_sandbox.py`)

1. `_create`(`:1172-1190`):`"mountPath": WORKSPACE_ROOT` → `NAS_MOUNT`。
2. `_chown_workspace_mount`(`:752`):`chown … WORKSPACE_ROOT` → `NAS_MOUNT`(仍 best-effort)。
3. **新增** `_ensure_exec_view_dir(sbx)`:post-create、`just_created` 时以 root 跑 `mkdir -p /workspace`;**失败即 post-create 失败**(与 seed_files 同一段 try,沙箱被拆)。镜像不动的代价就是这一句。
4. exec(`:1466-1500`):命令串换成 `build_exec_command(agent_key, script)`,`cwd=NAS_MOUNT`(进 ns 之前的 cwd 无所谓,但不能是不存在的路径)。
5. `layout` 字段(§4.6)翻成 `agent-ns`。
6. seed_files 写 `/opt/skills/<key>/…`(`:681-690`),不在挂载下,不动。

### 4.5 本地 supervisor 后端

- `runtime_provider.py`:`--volume {vol}:/workspace` → `:/mnt/workspace`;`--tmpfs /workspace:…` → `/mnt/workspace:…`;
  `--workdir /workspace` → `/mnt/workspace`;**新增** `--tmpfs /workspace:ro,size=4k`(bind 目标);**新增** `--security-opt apparmor=unconfined`。
- `runner.py`:子进程改为 §4.3 的 argv;`run_once(code, timeout_s, envs, agent_root)`;不再 `os.makedirs`、不传 `cwd`。
- supervisor 线协议:`ExecRequest.cwd` → `agent_root`(`schemas.py:106` / `runner_link.py` / `supervisor.py:393-424` / `app.py:343`);
  `HTTPSupervisorRuntime.exec` 绑了 agent 时发 `agent_root=agent_nas_root(key)`,未绑不发。
- **seccomp(`infra/sandbox-image/seccomp-profile.json`)加两条**,都不带 `includes.caps`:
  `unshare` 只放 `flags & ~(CLONE_NEWUSER|CLONE_NEWNS) == 0`(`SCMP_CMP_MASKED_EQ`,mask `4026400767`);
  `mount` + `open_tree` `move_mount` `fsopen` `fsconfig` `fsmount` `fspick` `mount_setattr`。
  **不放** `umount2` / `setns` / `pivot_root`(仍 cap 门控)。`test_seccomp.py` 的 `test_privileged_syscalls_only_cap_gated` 去掉 `mount`/`unshare`,加三条新闸。
- **钉住的 profile 成为本地后端的硬前提**:`app.py:164` 启动时 `seccomp_profile_path is None` → 拒绝启动(信息写明 B-60 与原因)。
  compose 把 `infra/sandbox-image/seccomp-profile.json` 挂进 supervisor 容器固定路径
  `./sandbox-image/seccomp-profile.json:/etc/expert-work/seccomp-profile.json:ro`,并设
  `EXPERT_WORK_SANDBOX_SECCOMP_PROFILE_PATH=/etc/expert-work/seccomp-profile.json`(见 §零 第 10 条修订——
  docker CLI 是**客户端**读这个文件、把 JSON 内联进发给 daemon 的 HostConfig,路径只需要在
  **supervisor 容器里**能读到,不是宿主路径);验收套件 fixture 显式传仓内 profile 路径。
- supervisor 线协议 `ExecRequest` 是 pydantic 默认的 `extra="ignore"`:老 orchestrator 发不带
  `agent_root` 的旧请求给新 supervisor 时,新字段静默不存在,exec 悄悄退回未绑定行为、不报错 ——
  只影响本地 supervisor 后端(dev/CI);ACS 路径不经这层 HTTP schema,走 `build_exec_command`
  直接拼命令串,不受影响。**发布注记**:dev 环境 orchestrator 与 supervisor 必须同批部署,
  不能只升级一边。
- **AppArmor**:`docker-default` 有 `deny mount,`,所以 `apparmor=unconfined`;Ubuntu 24.04 的
  `kernel.apparmor_restrict_unprivileged_userns=1` 会让 unconfined 建不了 userns → `ci.yml` 集成 job 与 `sandbox-gvisor.yml`
  各加一步 `sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0`(有这个键才设)。**未实测,CI 是判据**。
  红了的处理分支:① 把 sysctl 步骤位置/条件修对;② 若 runner 已把 `userns` 写进 `docker-default`、只缺 `mount`,改为
  加载一份自定义 AppArmor profile(`docker-default` 去掉 `deny mount,` 加 `userns,`)并 `--security-opt apparmor=<name>` —— 停下拍板,不糊。
- macOS Docker Desktop:无 AppArmor,`apparmor=unconfined` 被接受(实测);seccomp 同上必配。
- runsc:gVisor 实现了 user ns + bind + tmpfs,但**本仓库没实测过**。`Acceptance suite under runsc`(`.github/workflows/sandbox-gvisor.yml`)
  红了就是答案。若红:runsc 档「跳过命名空间用例」**不是选项**(那等于接受 CI 沙箱与生产沙箱行为不同);停下拍板(换 runsc 版本 / 换 CI 运行时)。

### 4.6 热会话的布局版本

旧热会话的 NAS 还挂在 `/workspace`,新命令串在它上面第一条 bind 就会失败(fail-closed,exec 报错)。处理:

- `sandbox_instance` 加列 `layout TEXT NOT NULL DEFAULT 'user-root'`(迁移 `0155`,expand-only)。
- `SandboxInstanceStore.claim_warm(..., layout)` / `create_ephemeral(..., layout)` 由**调用方**传入本进程认得的布局并写进 INSERT;
  `claim_warm` 输家分支返回 **四元组** `(winner_id, container_id, acquired_at, layout)`(与 `acquired_at` 同一次 SELECT 顺带取出,零额外往返)。
  两个字面量 `SANDBOX_LAYOUT_USER_ROOT = "user-root"` / `SANDBOX_LAYOUT_AGENT_NS = "agent-ns"` 定义在
  `expert_work.persistence.sandbox_instance_store`,与迁移的 `server_default` 同源。
- `AgentSandboxClient.layout: str` 字段:先落列+闸的 PR 里默认 `user-root`(零行为变化),exec 改动的 PR 翻成 `agent-ns`。
- `acquire` 拿到 `winner_layout != self.layout` 的会话 → 走与年龄封顶**同一条**重建路径(`destroy` 真 kill + 清行 + 重占坑 + create),
  `destroy_reason = "layout_mismatch"`(`_LAYOUT_MISMATCH_DESTROY_REASON`,与 `_WARM_RECONNECT_DESTROY_REASON` / `_WARM_AGE_DESTROY_REASON` 并列)。
  年龄封顶那段抽成「`rebuild_reason` 非空则重建」,两种原因共用一段,不复制。
- 发布时**不需要**手工清池:第一次 acquire 自动换代。本地 supervisor 的温池随 supervisor 进程重启一起换代,不需要列。

### 4.7 提示词与工具描述

`agent_factory.py:1737` 那句「the sandbox working directory is /workspace」**保留**,它现在是真的。
`list_dir` 等描述里的 `/workspace` 锚不动。**不加**「请写相对路径」之类的劝导 —— 边界在文件系统上,不靠劝。
`bash` / `exec_python` 描述若提「隔离」,按 §五 口径改写成「默认视图里只有你自己的目录」。

### 4.8 拆旧补丁

#1551 在 `save_artifact` 里加的「用户根认领」分支(`file_ops._ARTIFACT_LOCATE_MAIN` 的
`claimed_from_user_root` 路径、`artifact.py` 对应话术与测试、`user_ws`/`agents_dir`/`shared_dir` 三个参数)**拆掉**:
exec 已经写不到用户根,留着就是一段带着洞形状的死代码。存在性校验(`not_found` / `not_a_file` / `path_escapes_workspace`)**留着**;
`forbidden_scope` 随认领一起删(视图里没有别人的目录可命中)。

## 五、并发与安全性论证

- 命名空间按进程树:同一沙箱里 agent A 与 agent B 的 exec 各在自己的 mount ns,
  A 的 `/workspace` 与 B 的 `/workspace` 是两个不同的 bind。两处 spike 都证外面看不到里面。
- `bash` 持 per-workspace 写锁(`bash.py:55`),`exec_python` 不持 —— 与本方案无关,不改。
- user ns 内的「root」对外仍是 uid 10000:写出的文件属主 10000(两处实测),NAS 上 `0o700`/`umask 077` 语义不变。
- 能不能逃:ns 内能不能把 `/mnt/workspace` 上的 tmpfs 挪开露出用户根 —— **ACS 上能**(无 seccomp,`umount`/`mount --move` 都在);
  本地后端 `umount2` 仍 cap 门控,但 `mount --move` 走 `mount(2)`,同样能。所以本方案对**故意**的代码仍不是绝对边界;
  它保证的是**照平台教法写的代码**不可能越界,以及**默认视图**里别的 agent 不存在。与 `/opt/skills/<key>` 今天的强度一致。
  写进工具描述与对外文档时按这个口径说,不说「隔离」。
- 放行 `unshare(CLONE_NEWUSER)` 在 runc 上扩大了内核攻击面(user namespace 是历史上 CVE 高发区)。本地后端只用于 dev / CI;
  生产是 ACS microVM(内核边界在 microVM 上,沙箱里本来就没有 seccomp)。runsc 上 userns 在 sentry 里模拟,不触及宿主内核。
- 绑了 agent 时裸 `shared/…` 相对路径在视图里撞只读 bind —— 做成保留首段(§4.2),不是静默 EROFS。

## 六、测试

| 层 | 用例 |
|---|---|
| 单元(`test_exec_view.py`) | `EXEC_VIEW_SCRIPT` 逐字;argv 前缀;`build_exec_command` 绑/未绑两形态逐字;`shlex` 引号;空 key 与坏 key 拒 |
| 单元(`test_workspace_paths.py`) | `agent_nas_root` / `agent_view_alias` / `resolve_scope` 指向 `EXEC_VIEW`;坏 key 拒 |
| 单元(`test_file_ops.py` / `test_artifact_tools.py`) | `ws` = `/workspace`;折叠不变;`shared/` 保留段;认领分支删除后用户根同名文件 → `not_found` |
| 单元(`test_agent_sandbox.py`) | `_create` mountPath;post-create `mkdir -p /workspace` 以 root 跑且失败即拆;exec 命令串逐字;`layout_mismatch` 重建;同 layout 复用 |
| 单元(`test_seccomp.py`) | `unshare` 只在 mask 内放行;新挂载 API 无 cap 放行;`umount2`/`setns`/`pivot_root` 仍门控;`None` 路径启动拒绝 |
| 单元(supervisor / runtime) | `agent_root` 线协议三条;argv:`/mnt/workspace` 卷、`/workspace` 只读 tmpfs、workdir、apparmor |
| 漂移闸(契约文件末尾,无 marker) | runner.py 的脚本字面量 == `EXEC_VIEW_SCRIPT`;argv 前缀 == `EXEC_VIEW_ARGV_PREFIX`;runtime 包两常量 == orchestrator 两常量;`SANDBOX_PYTHON_FLAGS` 闸改成在 argv 里**找** `sys.executable` |
| 契约(`test_sandbox_runtime_contract.py`,两后端) | 绑定 exec 内 `os.stat('/workspace')` 与未绑 exec 内 `os.stat('/workspace/agents/<key>')` 的 `(st_dev, st_ino)` 相等;写 `/workspace/x` → 未绑 exec 在 `agents/<key>/x` 读到;绑定 exec 内 `os.listdir('/mnt/workspace') == []`;`/workspace/shared` 只读;两个不同 key **并发** exec 各只见自己的文件;未绑 exec 看到整个用户根 |
| 验收(`test_supervisor_integration.py`,docker runc + runsc) | 同上跑真容器 |
| 集成(persistence) | `claim_warm` 写入并返回 layout |
| 真栈(测试环境) | 金丝雀 PASS;探针用户复现 §一:`exec_python` 写 `/workspace/x.pptx` → `save_artifact` → 下载得到,且结果里**没有**「moved into your agent workspace」;`sandbox_instance` 里旧行以 `layout_mismatch` 销毁、新行 `agent-ns` |

## 七、发布

1. PR 顺序:docs(本 spec + 计划)→ 持久层列+闸(零行为变化)→ 写入侧全量(一次合)。
2. 合并 → `release.sh test` → 热会话自动换代 → 探针 + 测试人员。**滚动窗口有界抖动**(§零 第 11 条):
   control-plane 生产 2 副本、默认 RollingUpdate,写入侧全量那次合并翻转 `layout` 默认值之后,
   窗口期(约 1-2 分钟)内新旧 pod 对同一批热会话的期望布局不一致,双方都可能把对方刚建的热会话
   判成 `layout_mismatch` 销毁重建,可能连带打断另一侧正在跑的 exec。**拍板(2026-09-14)**:
   不改部署形状 —— 不上 Recreate、不缩容到零、不改发布策略,接受为有界抖动,只剩一个版本后自愈。
3. 生产按执行单三段 A / B1 / C / B2;B1、B2 钉子重定为含本 spec 的提交。
4. 执行单 `docs/runbooks/2026-09-14-prod-release-checklist.md` 改期,原「今晚 18:00」作废。
5. 沙箱镜像**不随本次改动**;B-59 刷钉子时镜像仍**不得**预建 `/workspace`(ACS 老代码 + 新镜像 = symlink 建不上)—— 运行期 `mkdir -p` 是规则,不是过渡。
