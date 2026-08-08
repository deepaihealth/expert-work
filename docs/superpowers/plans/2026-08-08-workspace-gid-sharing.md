# 工作区跨 uid 权限 实施计划(W2-BUG-1)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **⚠️ 本计划在 Task 3 之后改过方向。** 原方案是「control-plane 与沙箱共享 gid
> 10000」(setgid + chgrp + `supplementalGroups`),Task 1/2/3 已按它实施并合入
> 本分支。现方案是**两侧统一 uid 10000**——更简单、权限更紧,而且不需要共享组、
> setgid、chown、装配期闸中的任何一样。理由与代价见
> `docs/superpowers/specs/2026-08-08-workspace-gid-sharing-design.md` **§ 六**
> (那一节是本计划的第一手依据,先读它)。
>
> 所以 Task A 的主体是**拆掉已经落地的 gid 那套**,同时**保住 fix loop 里挣出来
> 的、与 gid 无关的独立改进**。别把两者一起删掉——那几条是三轮 review 换来的。

**Goal:** 修掉「agent 用 `write_file`/`edit_file`/状态投影写的文件一律 `0600`、用户在前端列得出却下载报 404」这条 Important(W2-BUG-1),做法是让 control-plane 与沙箱 agent 使用同一个 uid,从根上消除跨 uid 访问。

**Architecture:** control-plane 镜像的 uid 由 10002 改为 10000(与沙箱镜像 `agent` 用户一致)。两侧同属主之后,工作区目录收紧到 `0700`、文件保持 `0600`,`world-writable` 与 `world-readable` 一起退场。另外保留权限失败的独立错误归因——那与 uid 方案正交,是这条链上第二个真问题。

**Tech Stack:** Python 3.12(orchestrator + control-plane)、pytest、Docker、kustomize/k8s manifests。

## Global Constraints

- **统一 uid = 10000**,真值由 `infra/sandbox-image/Dockerfile` 的 `RUN useradd -u 10000 -m -s /usr/sbin/nologin agent` 决定。control-plane 侧是 `services/control-plane/Dockerfile:55` 的 `useradd --uid 10002`,改成 `10000`。**两处必须由漂移闸双向钉住。**
- 工作区目录 mode **`0o700`**;leaf 文件 **`0o600`**(即默认,不需要显式 chmod);`{tenant}/.deleted/` 保持 **`0o700`**。**不再有任何 `chown`/`fchown`/setgid。**
- 用户可见的错误文案**不含**路径、uid、mode;诊断只进结构化日志。
- 404「隐藏存在性」的既有安全姿态不变——只把"权限失败"这一种从 404 里拆出来。
- 仅改 control-plane 的 uid。**不动** credential-proxy(10001)、sandbox-supervisor(10003)。
- repo 风格:长的简体中文 docstring/注释,讲 WHY。

---

### Task A: 方向切换——拆掉 gid 那套,统一 uid,保住独立改进

**Files:**
- Modify: `services/control-plane/Dockerfile`(uid 10002 → 10000)
- Modify: `packages/expert-work-persistence/src/expert_work/persistence/workspace/layout.py`(删三常量)
- Modify: `packages/expert-work-persistence/src/expert_work/persistence/workspace/__init__.py`、`.../persistence/__init__.py`(删 re-export)
- Modify: `infra/k8s/base/control-plane/deployment.yaml`(删 `securityContext`)
- Modify: `services/control-plane/src/control_plane/runtime.py`(删装配期闸)
- Modify: `services/control-plane/tests/test_sandbox_backend_factory.py`(删闸的测试)
- Modify: `services/orchestrator/src/orchestrator/tools/file_ops.py`(删 `chmod 0640`)
- Modify: `services/orchestrator/src/orchestrator/tools/nas_workspace_store.py`(删 chown/setgid;mode → `0o700`;**保留错误归因**)
- Modify: `services/orchestrator/src/orchestrator/tools/agent_sandbox.py`(`_ensure_workspace_dir` → `0o700`;`_chmod_workspace_mount` 改 `chown`)
- Modify: `services/orchestrator/tests/test_nas_workspace_store.py`、`tests/test_file_ops.py`、`tests/test_agent_sandbox.py`
- Rewrite: `services/orchestrator/tests/test_workspace_shared_gid.py` → 改造成两份 Dockerfile 的 uid 漂移闸(文件可改名为 `test_workspace_shared_uid.py`)

**Interfaces:**
- Consumes: 无(这是拆除 + 收敛)。
- Produces: `orchestrator.tools.sandbox.WorkspacePermissionError` 继续存在,签名不变——Task B 的端点 import 它。

- [ ] **Step 1: 先读三份东西,再动手**

1. spec **§ 六**(方向变更那一节)——它列了确切的「保留 / 作废 / 改造」三张清单,本 task 就是执行它。
2. `git diff 5557f5e9..HEAD -- services packages infra` ——看清 gid 方案到底落了哪些东西。
3. `git stash list` / `git stash show -p stash@{0}` ——那是被中止的 Task 3 fix round 3,里面**有一条值得捞**:`user_root.is_dir()` 改 `os.stat` + `stat.S_ISDIR`(Python 3.14 上 `Path.is_dir()` 会吞掉 `OSError`,而 `pyproject.toml` 的 `requires-python = ">=3.12"` 允许 3.14,那会让 `list_files` 静默返回 `[]`)。其余部分是 gid 方案专属,作废。**不要直接 `git stash pop`**——大半内容要丢,手工捞那一条。

- [ ] **Step 2: 改 uid,并把漂移闸改造成钉两份 Dockerfile**

`services/control-plane/Dockerfile:55`:

```dockerfile
# 沙箱迁移波 2 BUG-1 —— 与沙箱镜像的 `agent` 用户同 uid(infra/sandbox-image/
# Dockerfile 的 `useradd -u 10000`)。两个进程读写同一棵 NAS 工作区树,uid 不同
# 时 POSIX 直接拒绝:agent 写的文件属主是 10000,本进程读不动,用户在前端列得出
# 却下载报 404。同 uid 之后跨 uid 这件事从根上不存在,工作区目录因此还能收紧到
# 0700(此前是 world-writable 的 0777)。两处 uid 由
# tests/test_workspace_shared_uid.py 双向钉住。
RUN useradd --uid 10000 --no-create-home --shell /usr/sbin/nologin expert_work
```

`COPY --chown=expert_work:expert_work` 用的是用户名不是数字,自动跟着,不用改。

漂移闸改造(原 `test_workspace_shared_gid.py`)——**双向**比对两份 Dockerfile,并且**不要**把数字写死在断言里(写死就只钉住一边):

```python
def test_control_plane_and_sandbox_images_share_one_uid() -> None:
    """两份 Dockerfile 的 uid 必须相同 —— 这是 W2-BUG-1 的唯一防线。

    两个数字一旦分叉,症状就是 BUG-1 原样复发:agent 写的文件 control-plane 读
    不动,前端列得出、下载 404,而且不会有任何一条日志说"是 uid 不一样"。两份
    Dockerfile 分属不同目录、不同发布线(沙箱镜像走 sandbox-image.yml,
    control-plane 走 release.sh),漂移是迟早的事。

    刻意不打 ``@pytest.mark.integration``、也刻意不 skip:漂移闸在跳过时就等于
    不存在,而这两个文件在仓库 checkout 里必然存在。
    """
```

- [ ] **Step 3: 跑闸,确认它在改 uid 之前是红的**

先把 control-plane 的 uid 改回 `10002` 跑一次,确认闸红;再改回 `10000`,确认绿。一个新写的闸不自己验一次红,就只是一句祝愿。

- [ ] **Step 4: 拆掉 gid 那套**

按 spec § 六「作废」清单逐项删:三个常量与两处 re-export、`deployment.yaml` 的 `securityContext`、`runtime.py` 的装配期闸与 `_process_is_in_shared_gid`、`nas_workspace_store.py` 的 `_chgrp_denied_level`、`file_ops.py` `_atomic_write` 里的 `os.chmod(tmp, ...)`、所有 `chown`/`fchown` 与 setgid,以及它们各自的测试。

删 `_atomic_write` 的 chmod 时把那段注释一起删干净:它整段在讲"另一个 uid 读不到",在新方案下这个前提没了。**留着一段前提已经不成立的 WHY 注释,比没有注释更坏**——这条在本任务里已经吃过三次亏。

- [ ] **Step 5: 目录 mode 收到 `0o700`**

`nas_workspace_store.py` `_openat_dir` 的 `os.fchmod(fd, ...)`、用户根创建处、`agent_sandbox.py` `_ensure_workspace_dir` 的 `os.chmod(path, ...)` —— 全部 `0o700`,不带 chown。

顺带把这几处的 docstring 重写:现存的长篇论证(「跨 uid 改属主在非 root 下做不到,退而求其次用宽 mode」「宽 mode 是刻意取舍不是疏忽」)在新方案下**全部作废**,而且会把下一个读者引回我们刚走出来的死胡同。删掉,换成一句「两侧同 uid,所以 0700 即可」。`# noqa: S103` 也可以摘了。

- [ ] **Step 6: `_chmod_workspace_mount` 改语义并改名**

它现在以 `user="root"` 跑 `chmod 0777`。新方案下它的职责变了:平台自动创建 subPath 目录时属主是 `root:root 0755`(集群实测),那条兜底路径下 agent 仍写不进去 —— 所以改成 `chown 10000:10000`。名字 `_chmod_...` 名不副实,一并改。

保持 best-effort(失败只记日志不抛):control-plane 正常先建好目录时属主已经是 10000,平台看到已存在就不会重建,这条兜底几乎不触发。

- [ ] **Step 7: 保住独立改进,逐条确认还在**

这五条与 uid 方案正交,是三轮 review 换来的,**删 gid 代码时很容易连带删掉**。逐条 grep 确认仍在且仍有测试:

1. `WorkspacePermissionError`(`orchestrator/tools/sandbox.py`)与 `read_file`/`write_file`/`delete_file`/`list_files` 里的窄接。**`except PermissionError` 必须排在 `except OSError` 之前**,否则是死代码。
2. `write_file` 的错误边界包住 `with os.fdopen(...)` 的 close/flush —— `BufferedWriter` 让小文件的 `ENOSPC` 在 close 才爆,不包住就漏 bare `OSError`。
3. `list_files` 的 `os.walk(onerror=...)` —— 不加它,一棵读不动的子树被静默丢掉。
4. 建 tenant 子树的 EACCES 归到 `WorkspacePermissionError`(不是通用类型)——那是运行手册第一步的失败,也是本次的旗舰诊断。
5. 从 stash 捞 `user_root.is_dir()` → `os.stat` + `S_ISDIR`。

- [ ] **Step 8: 跑全套**

```
cd services/orchestrator && DOCKER_HOST= uv run pytest -m "not integration"
cd services/control-plane && DOCKER_HOST= uv run pytest
cd packages/expert-work-persistence && DOCKER_HOST= uv run pytest
```
Expected: 全绿(control-plane 侧 `test_eval_engine_live.py` 的 6 条 `ModuleNotFoundError: tools` 是既有失败,与本改动无关,`git stash` 对照确认过)。ruff / ruff-format / CI-scope mypy 全清。

`kubectl kustomize infra/k8s/overlays/test | grep -c supplementalGroups` 应为 0。

- [ ] **Step 9: 提交**

```bash
git add -A
git commit -m "refactor(workspace): 共享 gid 改为两侧统一 uid 10000,目录收紧到 0700"
```

---

### Task B: 下载 / 删除 / 列表端点——权限失败不再伪装成 404

**Files:**
- Modify: `services/control-plane/src/control_plane/api/sessions.py`(`download_session_workspace_file`、`delete_session_workspace_file`)
- Modify: `services/control-plane/src/control_plane/api/workspace.py`(`/files`、`/file` GET、`/file` DELETE)
- Test: control-plane 侧对应测试文件

**Interfaces:**
- Consumes: `orchestrator.tools.sandbox.WorkspacePermissionError`(Task A 保留)。

- [ ] **Step 1: 写失败的测试**

两条,配对:

```python
async def test_workspace_download_reports_server_error_on_permission_denied(...) -> None:
    """store 抛 WorkspacePermissionError → 500,不是 404。

    404 的语义是"不存在 / 你不该知道它存在";权限读不动是**服务端配置问题**,
    塞进 404 会让用户看到"文件不存在"而它明明列在上一屏 —— W2-BUG-1 的诊断
    成本几乎全在这里。响应体不含路径 / uid / mode。
    """


async def test_workspace_download_still_404s_on_a_missing_file(...) -> None:
    """对照组:普通 SandboxSupervisorError 仍是 404。

    防止把"隐藏跨用户存在性"这条既有安全姿态一并改坏。
    """
```

- [ ] **Step 2: 跑,确认第一条红**

- [ ] **Step 3: 改端点**

每一处 `except SandboxSupervisorError` **之前**插窄的:

```python
        except WorkspacePermissionError as exc:
            logger.warning("session_workspace.permission_denied", exc_info=True)
            raise HTTPException(status_code=500, detail="workspace file unavailable") from exc
        except SandboxSupervisorError as exc:
            logger.warning("session_workspace.read_failed", exc_info=True)
            raise HTTPException(status_code=404, detail="file not found") from exc
```

`WorkspacePermissionError` 是 `SandboxSupervisorError` 的子类,**顺序反了就永远走不到**。

- [ ] **Step 4: `/files` 那处单独看——它现在把异常吞成成功**

`workspace.py:188-190` 把 `SandboxSupervisorError` 收成 `{"success": true, "files": []}`。Task A 让 `list_files` 在权限失败时抛,如果这里不改,**一次权限失败会渲染成"工作区是空的"**——比 404 更坏,用户连"出错了"都看不到。这一条是 Task A 的 co-ship 前提,不是可选项。

- [ ] **Step 5: 跑测试 + 提交**

```bash
git commit -m "fix(workspace): 端点把权限失败从 404/空列表里拆出来"
```

---

### Task C: 契约测试——落地权限进契约

**Files:**
- Modify: `services/orchestrator/tests/test_sandbox_runtime_contract.py`

- [ ] **Step 1: 加两条契约用例**

```python
@pytest.mark.integration
async def test_written_file_is_readable_by_the_control_plane_identity(...) -> None:
    """一套用例两实现:经 store 写的文件,本进程(= control-plane 身份)读得回来。

    **为什么这条要进契约套件**:W2-BUG-1 在 19/19 全绿的套件下活了下来,因为原
    套件只验"写进去读得出",而套件里写和读是同一个进程 —— 真实部署里写方是沙箱、
    读方是 control-plane。同 uid 方案让这两者重新变成同一个身份,这条用例钉的
    就是那个前提:哪天 uid 又分叉,它必须红。
    """


@pytest.mark.integration
async def test_user_workspace_root_is_not_world_accessible(...) -> None:
    """用户工作区根目录 mode 是 0o700 —— other 档全零。

    波 2 期间这里是 0o777(world-writable),靠 subPath 挂载范围做隔离,POSIX
    位不设防,并因此留下两条 dismissed 的 CodeQL high。同 uid 之后不需要给任何
    "另一方"开口子,这条断言防的是有人为了排查方便又把它放宽回去。
    """
```

两条都按该文件既有的 parametrize/fixture 结构在两个后端上跑。

- [ ] **Step 2: 跑契约套件 + 提交**

```
cd services/orchestrator && DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock uv run pytest tests/test_sandbox_runtime_contract.py -v -m integration
```

云后端那一档需要 `EXPERT_WORK_SANDBOX_*` 环境变量,没配会 skip;本地那一档必须真跑。

```bash
git commit -m "test(workspace): 契约套件补落地权限两条"
```

---

### Task D: 运行手册——存量迁移 + 发布顺序

**Files:**
- Modify: `docs/runbooks/sandbox-image-release.md`(与「波 2 首发步骤」放一起)

- [ ] **Step 1: 写迁移 + 发布顺序小节**

必须包含:

1. **为什么要迁移**:存量目录/文件属主是 10002(control-plane 建的)或 10000(沙箱建的),混着。uid 统一到 10000 之后,10002 那批新 control-plane 就不再是属主,`0700` 的目录会直接读不动。
2. **一次性 root Job 的完整 YAML**(挂 `workspace-nas`,照本文件既有临时 Pod 的写法),命令:

```sh
chown -R 10000:10000 /mnt/workspaces
find /mnt/workspaces -mindepth 2 -type d ! -path '*/.deleted*' -exec chmod 0700 {} +
find /mnt/workspaces -mindepth 3 -type f ! -path '*/.deleted/*' -exec chmod 0600 {} +
rm -rf /mnt/workspaces/_gidprobe /mnt/workspaces/_chgrpprobe
ls -la /mnt/workspaces/*/ | head -40
```

注意 `/mnt/workspaces` 根本身保持 `1777`(多租户子树平级摆放,sticky bit 防互删),`chown -R` 会连它一起改属主——确认这是想要的,或者把根排除掉。**实施时在测试环境先跑一遍再写进手册,不要写没跑过的命令。**

3. **顺序敏感,三步**:① 迁移 Job → ② `release.sh`(新 uid 的镜像)→ ③ 复验下载。为什么不能换序要写清楚:老镜像(uid 10002)配已经 chown 成 10000 的目录 = 立刻全挂,所以迁移和发布之间的窗口越短越好,最好连着做。
4. **为什么不做代码自愈**:自愈会让"目录属主归谁负责"变成两个答案。生产还没上线,这是一次性成本。

- [ ] **Step 2: 提交**

```bash
git commit -m "docs(runbook): 工作区 uid 统一的存量迁移 Job + 发布顺序"
```

---

## 真栈验收(全部任务完成后,人工执行)

顺序照运行手册:迁移 Job → release → 复验。

```
□ 迁移 Job 跑完,抽查一个用户目录:drwx------ + 属主 10000
□ control-plane pod 里 `id` = uid 10000
□ agent 用 write_file 写文件 → NAS 上 0600 属主 10000
□ 前端下载该文件 → 200,逐字节一致(BUG-1 的直接复现用例)
□ 前端下载 MEMORY.md → 200(存量文件,验迁移 Job)
□ agent read_document 读 control-plane 上传的文档 → 读得到(反方向)
□ agent 建子目录再写文件 → control-plane 列得出、下得动
□ 前端删除一个 agent 写的文件 → 成功
□ 软删闸仍生效(.deleted 未被波及)
□ 上传一个新文档(触发 control-plane 建目录)→ 目录 0700 属主 10000
```
