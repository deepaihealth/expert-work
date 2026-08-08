# 工作区跨 uid 权限:gid 共享 实施计划(W2-BUG-1)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 control-plane(uid 10002)与沙箱 agent(uid 10000)通过共享 gid 10000 读写同一棵 NAS 工作区树,修掉「agent 用 `write_file`/`edit_file`/状态投影写的文件一律 0600、用户前端下载报 404」这条 Important。

**Architecture:** 用户工作区目录改成 `2770` + group 10000(setgid 让新条目自动继承 group),leaf 文件从 `0600`/`0644` 改成 `0640`,control-plane Deployment 加 `supplementalGroups: [10000]`。world-writable(`0o777`/`0o666`)全部退场。权限失败从"文件不存在"里拆出来单独归因。

**Tech Stack:** Python 3.12(orchestrator + control-plane)、pytest、kustomize/k8s manifests。

## Global Constraints

- 共享 gid 的真值是 **10000**,由 `infra/sandbox-image/Dockerfile` 的 `RUN useradd -u 10000 -m -s /usr/sbin/nologin agent` 决定(`useradd` 默认建同名同 id 主组)。三处副本(persistence 常量 / orchestrator 用它 / k8s manifest)必须由漂移闸钉住。
- 用户工作区目录 mode 一律 **`0o2770`**;leaf 文件一律 **`0o640`**;`{tenant}/.deleted/` 保持 **`0o700`** 且**不**共享 gid。
- **每个目录都要显式 `chmod 2770`**,不能依赖 setgid 继承 —— 集群实测:`os.makedirs` 建的子目录是 `0o2755`(setgid 位和 group 继承了,权限位走 umask),group 没有 `w`。
- `os.chown(path, -1, 10000)`(只改 group)合法;`os.chown(path, 10000, -1)`(改 uid)非 root 恒 `EPERM`。已有 docstring 把两者混为一谈,改动时必须一并订正。
- 给用户看的错误文案**不含**路径、uid、mode 等细节;诊断信息只进结构化日志。
- 404「隐藏存在性」的既有安全姿态不动 —— 只把"权限失败"这一种从 404 里拆出来。
- spec:`docs/superpowers/specs/2026-08-08-workspace-gid-sharing-design.md`。

---

### Task 1: 共享 gid 常量 + 三方漂移闸 + Deployment supplementalGroups

**Files:**
- Modify: `packages/expert-work-persistence/src/expert_work/persistence/workspace/layout.py`
- Modify: `packages/expert-work-persistence/src/expert_work/persistence/workspace/__init__.py`(若它 re-export layout 的常量,补上新常量)
- Modify: `packages/expert-work-persistence/src/expert_work/persistence/__init__.py`(同上;`SANDBOX_SKILLS_ROOT` 怎么导出就怎么导出)
- Modify: `infra/k8s/base/control-plane/deployment.yaml`
- Test: `services/orchestrator/tests/test_workspace_shared_gid.py`(新建)

**Interfaces:**
- Produces: `expert_work.persistence.WORKSPACE_SHARED_GID: int`(= 10000)、`WORKSPACE_DIR_MODE: int`(= `0o2770`)、`WORKSPACE_FILE_MODE: int`(= `0o640`)。Task 2/3/4 全部从这里 import,不再写字面量。

- [ ] **Step 1: 写失败的漂移闸测试**

新建 `services/orchestrator/tests/test_workspace_shared_gid.py`:

```python
"""共享 gid 的三方漂移闸 —— 常量 / 沙箱镜像 / k8s manifest 必须同值。

gid 10000 的**事实源**是沙箱镜像里 ``agent`` 用户的主组(``useradd -u 10000``
默认建同名同 id 组),编排进程运行时读不到它,control-plane 的 Pod
``securityContext`` 里又必须写一份字面量 —— 于是同一个数字有三份副本。手法照
``test_image_env_matches_dockerfile``:刻意不打 ``@pytest.mark.integration``、
也刻意不 skip(漂移闸跳过时等于不存在),文件在仓库 checkout 里必然存在。
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml
from expert_work.persistence import WORKSPACE_SHARED_GID

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SANDBOX_DOCKERFILE = _REPO_ROOT / "infra" / "sandbox-image" / "Dockerfile"
_CP_DEPLOYMENT = _REPO_ROOT / "infra" / "k8s" / "base" / "control-plane" / "deployment.yaml"


def test_shared_gid_matches_the_sandbox_image_agent_uid() -> None:
    """镜像里 ``agent`` 的 uid(= 其主组 gid)就是我们共享的那个 gid。

    改了 Dockerfile 的 ``useradd -u`` 而没改常量 → control-plane 会 chgrp 到
    一个沙箱里不存在的组,跨 uid 读写当场全断。
    """
    text = _SANDBOX_DOCKERFILE.read_text(encoding="utf-8")
    match = re.search(r"useradd\s+-u\s+(\d+)\s+.*\bagent\b", text)
    assert match is not None, "沙箱 Dockerfile 里找不到 `useradd -u <uid> ... agent` 行"
    assert int(match.group(1)) == WORKSPACE_SHARED_GID


def test_control_plane_deployment_declares_the_shared_gid() -> None:
    """control-plane Pod 必须把共享 gid 列进 ``supplementalGroups``。

    漏了这一项 = 进程根本不在那个组里,``0o640`` 的文件一律读不了 —— 而且
    症状与本次修复之前一模一样(下载 404),极难归因。
    """
    doc = yaml.safe_load(_CP_DEPLOYMENT.read_text(encoding="utf-8"))
    groups = doc["spec"]["template"]["spec"]["securityContext"]["supplementalGroups"]
    assert groups == [WORKSPACE_SHARED_GID]
```

- [ ] **Step 2: 跑测试,确认两条都失败**

Run: `cd services/orchestrator && DOCKER_HOST= uv run pytest tests/test_workspace_shared_gid.py -v`
Expected: FAIL — `ImportError: cannot import name 'WORKSPACE_SHARED_GID'`

- [ ] **Step 3: 加常量**

`layout.py` 末尾(`WORKSPACE_RESERVED_PREFIXES` 之后)追加:

```python
#: 沙箱 agent 用户的主组(``infra/sandbox-image/Dockerfile`` 的
#: ``useradd -u 10000 ... agent``,``useradd`` 默认建同名同 id 主组)。
#:
#: **为什么控制面要认识它**:波 2 把工作区权威搬到 NAS 之后,同一棵目录树
#: 被两个不同 uid 的进程读写 —— control-plane(uid 10002,``services/
#: control-plane/Dockerfile`` 的 ``useradd --uid 10002 ... expert_work``)与
#: 沙箱里的 agent(uid 10000)。跨 uid 改属主在非 root 下做不到(``chown``
#: uid 恒 ``EPERM``),但**改 group 到自己所属的组是允许的**,而 Pod 的
#: ``securityContext.supplementalGroups`` 可以把 control-plane 放进这个组
#: —— 于是"共享一个 gid + 目录 setgid"成了两侧都能落地的唯一支点。
#:
#: 三份副本(本常量 / 镜像的 ``useradd`` / k8s Deployment 的
#: ``supplementalGroups``)由 ``test_workspace_shared_gid.py`` 双向钉住。
WORKSPACE_SHARED_GID = 10000

#: 用户工作区目录的 mode —— ``rwxrws---``。
#:
#: ``0o2770`` 的三段:属主(control-plane 或先建它的一方)与 group
#: (:data:`WORKSPACE_SHARED_GID`,即沙箱 agent)读写执行齐全,``other``
#: 全零。前导 ``2`` 是 **setgid**:目录里新建的文件/子目录 group 自动继承
#: 成 10000,写入方不需要(也没权限)自己 ``chown`` —— 这是整套方案的枢纽。
#:
#: **每个目录都要显式设成这个值,不能靠继承**:集群实测,``os.makedirs``
#: 建出来的子目录是 ``0o2755``(setgid 位与 group 继承了,权限位走 umask),
#: group 少了 ``w``,另一侧就写不进去。
WORKSPACE_DIR_MODE = 0o2770

#: 工作区里新建 leaf 文件的 mode —— ``rw-r-----``。group 可读即可满足
#: "一侧写、另一侧读";``other`` 全零。写方向由各自的目录写权限决定,不靠
#: 文件的 group ``w`` 位。
WORKSPACE_FILE_MODE = 0o640
```

- [ ] **Step 4: 按既有出口惯例 re-export**

先看 `SANDBOX_SKILLS_ROOT` 在 `workspace/__init__.py` 与 `persistence/__init__.py` 里是怎么导出的(`from .layout import ...` + `__all__`),照同样方式把三个新常量加进去。不要新造一种导出风格。

- [ ] **Step 5: Deployment 加 securityContext**

`infra/k8s/base/control-plane/deployment.yaml`,在 `spec.template.spec` 下、`containers:` **之前**插入:

```yaml
    spec:
      # 波 2 BUG-1 —— 工作区在 NAS 上被两个 uid 共读写:control-plane 是
      # uid 10002,沙箱里的 agent 是 uid 10000。把 10000 挂进本 Pod 的附加组,
      # 加上工作区目录的 setgid(见 WORKSPACE_DIR_MODE),两侧才能读到对方
      # 写的 0640 文件。集群实测:阿里云 NAS(NFSv3 AUTH_SYS)完整支持附加组
      # 与 setgid 目录。数字由 test_workspace_shared_gid.py 钉住。
      #
      # 顺序敏感:这一项与代码里的 0640 必须同一次 release 落地。先上代码后
      # 上本项 = control-plane 既不在组里、文件又不再 world-readable,全部
      # 下载当场失败。
      securityContext:
        supplementalGroups: [10000]
      containers:
```

- [ ] **Step 6: 跑测试,确认全绿**

Run: `cd services/orchestrator && DOCKER_HOST= uv run pytest tests/test_workspace_shared_gid.py -v`
Expected: 2 passed

- [ ] **Step 7: 确认 kustomize 仍能 build**

Run: `kubectl kustomize infra/k8s/overlays/test | grep -A 2 supplementalGroups`
Expected: 输出含 `supplementalGroups:` 与 `- 10000`

- [ ] **Step 8: 提交**

```bash
git add -A
git commit -m "feat(workspace): 共享 gid 常量 + 三方漂移闸 + control-plane supplementalGroups"
```

---

### Task 2: `_atomic_write` 落 0640 —— 直击 BUG-1 根因

**Files:**
- Modify: `services/orchestrator/src/orchestrator/tools/file_ops.py`(`_atomic_write`,约 141 行)
- Test: `services/orchestrator/tests/test_file_ops.py`

**Interfaces:**
- Consumes: Task 1 的 `WORKSPACE_FILE_MODE`。**注意**:`_atomic_write` 住在一段**发进沙箱执行的源码字符串**里,沙箱内的解释器 import 不到 `expert_work.persistence` —— 必须把常量在**拼接时**插值成字面量,不能在 snippet 里写 import。

- [ ] **Step 1: 写失败的测试**

`services/orchestrator/tests/test_file_ops.py` 追加。它验的是 snippet 源码文本(snippet 真正的执行发生在沙箱里,单测跑不到),外加一个真跑 `_atomic_write` 的行为测:

```python
def test_write_snippet_chmods_the_temp_file_before_rename() -> None:
    """``_atomic_write`` 必须在 ``os.replace`` 之前 chmod。

    ``tempfile.mkstemp`` 恒定 0600(它的安全契约,与 umask 无关),而
    ``os.replace`` 保留源 inode 的权限位 —— 所以不 chmod 的话,经这条路写出
    的**每一个**文件都是 0600,control-plane(另一个 uid)一律读不了。这正是
    W2-BUG-1:前端列得出、下载报 404。

    顺序也是承重的:chmod 必须在 replace **之前**。之后再 chmod 的话,目标
    文件在两个系统调用之间有一个 0600 的可观测窗口,读方正好撞上就是一次
    随机失败。
    """
    from orchestrator.tools.file_ops import build_write_wrapper

    src = build_write_wrapper("a.txt", "x")
    chmod_at = src.index("os.chmod(")
    replace_at = src.index("os.replace(")
    assert chmod_at < replace_at, "chmod 必须在 os.replace 之前"
    assert "0o640" in src


def test_atomic_write_lands_group_readable(tmp_path) -> None:
    """真跑一遍 snippet 里的 ``_atomic_write``,断言落地 mode 是 0640。

    上一条测的是源码文本(能防重构把 chmod 删掉),这一条测的是真实文件系统
    行为 —— 两条都要,文本断言挡不住"chmod 了但 mode 写错"。
    """
    import os
    import stat

    from orchestrator.tools.file_ops import build_write_wrapper

    target = tmp_path / "out.txt"
    src = build_write_wrapper("out.txt", "hello")
    exec(compile(src.replace('_P["ws"]', repr(str(tmp_path)), 1), "<snippet>", "exec"), {})
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o640
```

> 实施提示:第二条测试要能在本进程里跑通 snippet,取决于 `_snippet()` 生成的
> 源码结构(它把 `_PARAMS` 作为 JSON 字面量嵌进去)。**先读 `_snippet` 的实现
> 再写这条**;如果直接 `exec` 不可行(例如它 `print` 到 stdout 且依赖
> `_PARAMS` 的注入方式),就改成用 `json.dumps` 构造完整参数、`exec` 后从
> stdout 捕获 envelope。不要为了让测试好写而改生产代码的结构。

- [ ] **Step 2: 跑测试,确认失败**

Run: `cd services/orchestrator && DOCKER_HOST= uv run pytest tests/test_file_ops.py -k "chmod or group_readable" -v`
Expected: FAIL

- [ ] **Step 3: 改 `_atomic_write`**

`file_ops.py` 里那段 snippet 源码字符串:

```python
def _atomic_write(full, data):
    parent = os.path.dirname(full) or _WS
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        # mkstemp 恒定 0600(它的安全契约,与 umask 无关),而 os.replace 保留
        # 源 inode 的权限位 —— 不在这里放开的话,经这条路写出的每一个文件都是
        # 0600,另一个 uid(control-plane)一律读不了(W2-BUG-1)。chmod 在
        # replace **之前**:反过来的话目标文件会有一个 0600 的可观测窗口。
        # group 位是承重的 —— 目录 setgid 让 group 是共享的那个 gid。
        os.chmod(tmp, 0o640)
        os.replace(tmp, full)
    except BaseException:
        ...
```

字面量 `0o640` **不要手写**:在拼接 snippet 的那处用 `WORKSPACE_FILE_MODE`
插值(照 `_WORKSPACE_ROOT` / `_MAX_LIST_ENTRIES` 现有的插值方式),让 Task 1
的常量成为唯一事实源。若现有结构只支持通过 `_P` 传参,就把 mode 作为一个
`_P` 键传进去。

- [ ] **Step 4: 跑测试,确认通过**

Run: `cd services/orchestrator && DOCKER_HOST= uv run pytest tests/test_file_ops.py -v`
Expected: 全绿(含既有用例)

- [ ] **Step 5: 提交**

```bash
git add -A
git commit -m "fix(workspace): _atomic_write 落 0640 —— mkstemp 的 0600 是 BUG-1 根因"
```

---

### Task 3: NasWorkspaceStore —— 目录 2770+chgrp、leaf 0640、权限失败单独归因

**Files:**
- Modify: `services/orchestrator/src/orchestrator/tools/nas_workspace_store.py`
- Modify: `services/orchestrator/src/orchestrator/tools/sandbox.py`(新异常类型)
- Test: `services/orchestrator/tests/test_nas_workspace_store.py`

**Interfaces:**
- Consumes: Task 1 的 `WORKSPACE_SHARED_GID` / `WORKSPACE_DIR_MODE` / `WORKSPACE_FILE_MODE`。
- Produces: `orchestrator.tools.sandbox.WorkspacePermissionError(SandboxSupervisorError)` —— Task 5 的端点 import 它。

- [ ] **Step 1: 写失败的测试**

`test_nas_workspace_store.py` 追加:

```python
def test_created_dirs_are_setgid_and_group_shared(tmp_path) -> None:
    """``_openat_dir`` 建出来的目录必须是 2770 + group 共享 gid。

    setgid 是枢纽:目录带 s 位之后,两侧谁在里面新建文件,group 都自动是共享
    的那个 gid —— 而两侧都没有 chown 对方 uid 的权限。少了 setgid 就只能靠每个
    写入方自己 chgrp,沙箱那边根本做不到。
    """


def test_write_file_lands_group_readable(tmp_path) -> None:
    """``write_file`` 落地的文件 group 可读(0640),不是 0644 也不是 0600。"""


def test_read_file_reports_permission_denied_distinctly(tmp_path) -> None:
    """读不动 ≠ 不存在。

    W2-BUG-1 的诊断成本几乎全在这一条上:``PermissionError`` 被收成
    "workspace file not found",端点翻成 404,用户看到"文件不存在"而它明明
    列在上一屏 —— 只能靠翻服务端日志才诊断得出来。
    """
```

三条的实现细节:
- 第一条:调一次 `write_file`(它会经 `_openat_dir(create=True)` 建出用户根),
  然后 `os.stat` 用户根目录,断言 `stat.S_IMODE(...) == 0o2770`。gid 断言需要
  真能 chgrp 到 10000 —— **本机跑测试的用户几乎肯定不在 gid 10000 里**,所以
  gid 那半句用 `monkeypatch` 把 `os.chown` 换成 spy、断言它被以
  `(-1, WORKSPACE_SHARED_GID)` 调用,而不是去断言真实 st_gid。mode 那半句测真值。
- 第二条:`write_file` 后 `os.stat` 该文件,断言 `0o640`。
- 第三条:先 `write_file` 造出文件,再 `os.chmod(f, 0o000)`,然后
  `pytest.raises(WorkspacePermissionError)`。**注意**:以 root 跑测试时
  `0o000` 也读得动 —— 加 `pytest.mark.skipif(os.geteuid() == 0, ...)`,理由写进
  skip 消息。

- [ ] **Step 2: 跑测试,确认三条都失败**

Run: `cd services/orchestrator && DOCKER_HOST= uv run pytest tests/test_nas_workspace_store.py -k "setgid or group_readable or permission_denied" -v`
Expected: FAIL

- [ ] **Step 3: 加异常类型**

`orchestrator/tools/sandbox.py`,紧挨 `SandboxSupervisorError` 定义:

```python
class WorkspacePermissionError(SandboxSupervisorError):
    """工作区文件存在,但本进程的 uid/gid 读写不动它。

    与"不存在"必须分开:前者是**服务端配置问题**(共享 gid 没配上、存量文件
    没迁移、目录 mode 不对),后者才是用户输入问题。合并成一个之后,用户看到
    的是"文件不存在"而文件明明列在浏览列表里 —— W2-BUG-1 的诊断成本几乎全在
    这上面。

    仍是 :class:`SandboxSupervisorError` 的子类,既有的宽 ``except`` 一律不受
    影响;只有想区分的调用方(工作区下载端点)才需要单独接它。
    """
```

- [ ] **Step 4: 改三处**

1. `_openat_dir`(约 252 行):

```python
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dfd)
        # 波 2 BUG-1 —— 从 0o777 收紧到 2770 + 共享 gid。setgid 位让这个目录里
        # 新建的条目自动继承 group,两侧(control-plane uid 10002 / 沙箱 agent
        # uid 10000)因此都不需要 chown 对方的 uid(那在非 root 下恒 EPERM)。
        # fchmod/fchown 都作用在已经握着的 fd 上,不重走字符串路径 —— 理由同
        # 上方 docstring 里对 fchmod 的说明,fchown 同理。
        #
        # 顺序承重:**先 chown 后 chmod**。非特权进程 chown 会清 set-user/
        # group-id 位(Linux 对目录网开一面,但 NFS 服务端不保证照做),反过来
        # 写 setgid 就可能被下一句悄悄抹掉。集群探针实测走的就是这个顺序。
        os.fchown(fd, -1, WORKSPACE_SHARED_GID)
        os.fchmod(fd, WORKSPACE_DIR_MODE)
        return fd
```

`os.fchown(fd, -1, gid)` 只改 group;**改 uid 才是 EPERM**,改 group 到自己
所属的组是 POSIX 允许的(集群实测坐实)。同时把这个函数 docstring 里那段
论证 `0o777` 的文字整段改写 —— 它现在会误导人。

2. `_LEAF_FILE_MODE = 0o644` → 改成引用 `WORKSPACE_FILE_MODE`(删掉本地常量,
   或让它等于新常量并在注释里说明为什么去掉 `other` 档)。

3. `read_file._read` 的三处 `except OSError as exc: raise SandboxSupervisorError(f"workspace file not found: ...")`:
   在每一处之前先接 `PermissionError`:

```python
                try:
                    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dfd)
                except PermissionError as exc:
                    raise WorkspacePermissionError(
                        f"workspace file not readable: {path!r}"
                    ) from exc
                except OSError as exc:
                    ...
```

`PermissionError` 是 `OSError` 的子类,**顺序反了就永远走不到** —— 三处都要
先接窄的。同一处理也加到 `write_file` / `delete_file` / `list_files` 的
`OSError` 收口(写不动、删不动同样是配置问题,不是"不存在")。

- [ ] **Step 5: 跑测试,确认通过**

Run: `cd services/orchestrator && DOCKER_HOST= uv run pytest tests/test_nas_workspace_store.py -v`
Expected: 全绿(含既有用例;`0o777` → `0o2770` 会让若干既有断言需要跟着改 —— 改期望值,不要改实现)

- [ ] **Step 6: 提交**

```bash
git add -A
git commit -m "fix(workspace): NasWorkspaceStore 目录 2770+共享 gid、leaf 0640、权限失败单独归因"
```

---

### Task 4: agent_sandbox —— 挂载点与用户目录同步到新权限模型

**Files:**
- Modify: `services/orchestrator/src/orchestrator/tools/agent_sandbox.py`(`_ensure_workspace_dir` 约 841 行、`_chmod_workspace_mount` 约 672 行)
- Test: `services/orchestrator/tests/test_agent_sandbox.py`

**Interfaces:**
- Consumes: Task 1 的 `WORKSPACE_SHARED_GID` / `WORKSPACE_DIR_MODE`。

- [ ] **Step 1: 写失败的测试**

`test_agent_sandbox.py` 里已有 `test_acquire_chmods_the_mount_from_inside_the_sandbox`,它断言沙箱侧收到的命令是
`[(f"chmod 0777 {WORKSPACE_ROOT}", None, "root", None)]`。改这条的期望,并加一条新的:

```python
async def test_acquire_sets_setgid_and_shared_group_on_the_mount() -> None:
    """沙箱侧兜底命令必须同时设 2770 与共享 group。

    只 chmod 不 chgrp:control-plane 建的目录 group 还是 10002,沙箱(gid
    10000)读不到里面的 0640 文件 —— 方向与 BUG-1 相反,一样是坏的。
    只 chgrp 不 chmod:group 位可能没有 w,沙箱写不进去。
    """
```

断言沙箱收到的命令序列同时含 `chmod 2770` 与 `chgrp`/`chown :10000`(以实现
选定的写法为准),且都以 `user="root"` 跑。

再加一条 `_ensure_workspace_dir` 的:

```python
async def test_ensure_workspace_dir_sets_mode_and_shared_group(tmp_path, monkeypatch) -> None:
    """control-plane 侧建目录:mode 2770 + chgrp 到共享 gid,不改 uid。

    ``os.chown`` 只能传 ``(-1, gid)``。传 uid 会在真集群上恒 EPERM —— 老
    docstring 里"chown 不行所以只能 chmod 0777"那段结论管的是改 uid,不构成
    对 chgrp 的否定(集群实测两者分开验过)。
    """
```

用 `monkeypatch` spy `os.chown`,断言调用参数是 `(path, -1, WORKSPACE_SHARED_GID)`;
mode 断言真值 `0o2770`。

- [ ] **Step 2: 跑测试,确认失败**

Run: `cd services/orchestrator && DOCKER_HOST= uv run pytest tests/test_agent_sandbox.py -k "setgid or shared_group or chmods_the_mount" -v`
Expected: FAIL

- [ ] **Step 3: 改 `_chmod_workspace_mount`**

```python
        try:
            await sbx.commands.run(
                f"chmod {oct(WORKSPACE_DIR_MODE)[2:]} {WORKSPACE_ROOT} "
                f"&& chown :{WORKSPACE_SHARED_GID} {WORKSPACE_ROOT}",
                user="root",
            )
        except Exception:
            ...
```

方法名 `_chmod_workspace_mount` 现在名不副实(它还 chgrp)—— 改成
`_prepare_workspace_mount_permissions` 或类似,并同步 docstring:那段
"放开到 0o777"的论证整段作废,换成 setgid + 共享 gid 的理由。

- [ ] **Step 4: 改 `_ensure_workspace_dir`**

```python
        def _do() -> None:
            path.mkdir(parents=True, exist_ok=True)
            # 先 chown 后 chmod —— 非特权进程的 chown 会清 set-user/group-id
            # 位(Linux 对目录网开一面,NFS 服务端不保证照做),反序会让 setgid
            # 被悄悄抹掉。集群探针实测走的就是这个顺序。
            os.chown(path, -1, WORKSPACE_SHARED_GID)
            os.chmod(path, WORKSPACE_DIR_MODE)
```

顺序是承重的:**先 `chown` 后 `chmod`**。**这一条要用测试钉住调用顺序**
(spy 两个调用、断言先后),不能只靠注释 —— 反序的症状是 setgid 静默消失,
而目录看起来"权限对着呢",极难归因。

同时改写 841-870 那段 docstring:「跨 uid 改属主在非 root 下做不到,退而求其次
用宽 mode」这段推理已经被实测推翻(改 uid 不行,改 group 行),留着会让下一个
读者重复我们踩过的坑。

- [ ] **Step 5: 跑测试**

Run: `cd services/orchestrator && DOCKER_HOST= uv run pytest tests/test_agent_sandbox.py -v`
Expected: 全绿

- [ ] **Step 6: 提交**

```bash
git add -A
git commit -m "fix(workspace): 沙箱侧挂载点与用户目录同步到 2770 + 共享 gid"
```

---

### Task 5: 下载端点 —— 权限失败不再伪装成 404

**Files:**
- Modify: `services/control-plane/src/control_plane/api/sessions.py`(`download_session_workspace_file` 约 552 行、`delete_session_workspace_file`)
- Modify: `services/control-plane/src/control_plane/api/workspace.py`(`/files`、`/file` GET、`/file` DELETE 三处)
- Test: `services/control-plane/tests/test_sessions_workspace.py`(按仓库既有测试文件名为准)

**Interfaces:**
- Consumes: Task 3 的 `orchestrator.tools.sandbox.WorkspacePermissionError`。

- [ ] **Step 1: 写失败的测试**

```python
async def test_workspace_download_reports_server_error_on_permission_denied(...) -> None:
    """store 抛 WorkspacePermissionError → 500,不是 404。

    404 的语义是"不存在 / 你不该知道它存在";权限读不动是**服务端配置问题**
    (共享 gid 没配上、存量文件没迁),把它塞进 404 会让用户看到"文件不存在"
    而文件明明列在上一屏 —— W2-BUG-1 的诊断成本几乎全在这里。

    响应体不含路径 / uid / mode:那些只进结构化日志。既有的"404 隐藏跨用户
    存在性"姿态不变,只是把权限这一种拆出来。
    """
```

配一条对照测试:store 抛普通 `SandboxSupervisorError` 时仍是 404(防止把
"隐藏存在性"一并改坏)。

- [ ] **Step 2: 跑测试,确认失败**

Run: `cd services/control-plane && DOCKER_HOST= uv run pytest tests/ -k "permission_denied" -v`
Expected: FAIL(现在返回 404)

- [ ] **Step 3: 改端点**

每一处 `except SandboxSupervisorError` **之前**加窄的:

```python
        except WorkspacePermissionError as exc:
            logger.warning("session_workspace.permission_denied", exc_info=True)
            raise HTTPException(status_code=500, detail="workspace file unavailable") from exc
        except SandboxSupervisorError as exc:
            logger.warning("session_workspace.read_failed", exc_info=True)
            raise HTTPException(status_code=404, detail="file not found") from exc
```

`WorkspacePermissionError` 是 `SandboxSupervisorError` 的子类,**顺序反了就
永远走不到**。

- [ ] **Step 4: 跑测试**

Run: `cd services/control-plane && DOCKER_HOST= uv run pytest tests/ -k "workspace" -v`
Expected: 全绿

- [ ] **Step 5: 提交**

```bash
git add -A
git commit -m "fix(workspace): 下载/删除端点把权限失败从 404 里拆出来"
```

---

### Task 6: 契约测试补两条 —— 落地权限进契约

**Files:**
- Modify: `services/orchestrator/tests/test_sandbox_runtime_contract.py`

- [ ] **Step 1: 加两条契约用例**

```python
@pytest.mark.integration
async def test_write_file_lands_group_readable(workspace_store, ...) -> None:
    """一套用例两实现:经 store 写的文件,group 位可读。

    **为什么这条要进契约套件**:W2-BUG-1 在 19/19 全绿的套件下活了下来,因为
    原套件只验行为(写进去读得出),不验**落地权限** —— 而套件里写和读是同一个
    进程同一个 uid,权限永远不构成障碍。跨 uid 才是真实部署形态,那里权限就是
    行为。断言 mode 而不是"能不能读",正是为了让单进程的套件也能挡住它。
    """


@pytest.mark.integration
async def test_user_workspace_root_is_setgid(workspace_store, ...) -> None:
    """用户工作区根目录带 setgid 位。

    没有 setgid,新条目的 group 归写入方的主组 —— 两侧主组不同(10002 / 10000),
    而谁都没有 chown 对方 uid 的权限,于是每写一个新文件就多一个对方读不了的
    文件。setgid 是这套方案唯一不需要双方协作的支点。
    """
```

两条都要在两个后端上跑(照该文件既有的 parametrize/fixture 结构)。本地
supervisor 后端上同样成立 —— snippet 是共用的,目录 mode 由同一套常量决定。

- [ ] **Step 2: 跑契约套件**

Run: `cd services/orchestrator && DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock uv run pytest tests/test_sandbox_runtime_contract.py -v -m integration`
Expected: 既有 19 条 + 新 2 条全绿(云后端那档需要 `EXPERT_WORK_SANDBOX_*` 环境变量,没配会 skip —— 本地这一档必须真跑)

- [ ] **Step 3: 提交**

```bash
git add -A
git commit -m "test(workspace): 契约套件补落地权限两条 —— 原套件漏掉 BUG-1 的原因"
```

---

### Task 7: 运行手册 —— 存量迁移 Job + 发布顺序

**Files:**
- Modify: `docs/runbooks/sandbox-image-release.md`(或 `docs/runbooks/deployment.md`,取决于波 2 首发步骤住在哪 —— 与那节放一起)

- [ ] **Step 1: 写迁移 + 发布顺序小节**

内容必须包含:

1. **为什么要迁移**:存量文件多数已经是 group 10000(属主 `agent:agent`),
   缺的只是 `g+r` 位与目录的 setgid;control-plane 建的目录/文件 group 是
   10002,需要 chgrp。不迁移的话新代码写的 `0640` 文件 group 归 10002,
   control-plane 读得到但沙箱读不到 —— 方向与 BUG-1 相反,一样是坏的。
2. **一次性 root Job 的完整 YAML**(挂 `workspace-nas`,照本文件既有临时 Pod
   的写法),命令:

```sh
find /mnt/workspaces -mindepth 2 -maxdepth 2 -type d ! -name .deleted \
  -exec chgrp -R 10000 {} + -exec chmod -R g+r {} +
find /mnt/workspaces -mindepth 2 -type d ! -path '*/.deleted*' \
  -exec chmod 2770 {} +
rm -rf /mnt/workspaces/_gidprobe /mnt/workspaces/_chgrpprobe
ls -la /mnt/workspaces/*/ | head -40
```

3. **顺序敏感,三步**:① 迁移 Job → ② `release.sh`(manifest 的
   `supplementalGroups` 与代码的 `0640` 同一次落地,Deployment 更新是原子的)
   → ③ 复验下载。**为什么不能换序**要写清楚,别只写"按顺序执行"。
4. **为什么不做代码自愈**:自愈会让"目录权限归谁负责"变成两个答案,下次
   出问题得同时排查两条路径。生产还没上线,这是一次性成本。

- [ ] **Step 2: 提交**

```bash
git add -A
git commit -m "docs(runbook): 工作区 gid 共享的存量迁移 Job + 发布顺序"
```

---

## 真栈验收(全部任务完成后,人工执行)

按 spec § 五的清单,在测试环境跑。**顺序照运行手册**:迁移 Job → release → 复验。

```
□ 迁移 Job 跑完,抽查一个用户目录:drwxrws--- + group 10000
□ agent 用 write_file 写文件 → NAS 上 0640 group 10000
□ 前端下载该文件 → 200,逐字节一致(BUG-1 的直接复现用例)
□ 前端下载 MEMORY.md → 200(存量文件,验迁移 Job)
□ agent read_document 读 control-plane 上传的 0640 文档 → 读得到(反方向)
□ agent 建子目录再写文件 → control-plane 列得出、下得动
□ 前端删除一个 agent 写的文件 → 成功(目录 group 有 w)
□ 软删闸仍生效(.deleted 未被波及)
```
