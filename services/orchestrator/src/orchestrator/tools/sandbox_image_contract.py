"""沙箱**镜像**侧的契约常量 —— 两个 ``SandboxRuntime`` 后端共同的事实源。

从 :mod:`orchestrator.tools.agent_sandbox` 原样搬出(本分支第三次同类拆分,
前两次是 :mod:`orchestrator.tools.e2b_patch` 与
:mod:`orchestrator.tools.sandbox_instance_store`):那个模块又顶到仓内 800 行
的单文件硬上限,而这一块是里面最独立的一刀——通篇是"镜像/``runner.py`` 声明
了什么"的常量与理由,一行可执行代码、一处对 ``AgentSandboxClient`` 内部状态的
引用都没有。**不改任何一个值**,连 docstring 都逐字照搬。

住在一起的理由不只是行数:这几个常量的共同点是它们的事实源都**不在**编排
进程里(``infra/sandbox-image/Dockerfile`` 的 ``ENV``/``WORKDIR``、
``infra/sandbox-image/runner.py`` 的三个 clamp/截断常量、镜像里的
``USER agent``),运行时读不到,只能是第二份副本 + 一道漂移闸。对应的闸:
``test_image_env_matches_dockerfile``(双向比 Dockerfile)与
``test_exec_contract_constants_match_the_sandbox_image``(比 ``runner.py``
与 supervisor 的 HTTP 边界)。

``agent_sandbox`` 仍然 ``import`` 并使用它们全部,老的
``from orchestrator.tools.agent_sandbox import WORKSPACE_ROOT`` 之类照常可用。
"""

from __future__ import annotations

#: 沙箱内工作区挂载点 —— 与 supervisor 实现一致。
WORKSPACE_ROOT = "/workspace"

#: 沙箱镜像里 ``HOME``/``MPLCONFIGDIR`` 的落点(沙箱迁移波 2 Task 9,
#: 2026-08-07)。此前 ``HOME`` 就是 :data:`WORKSPACE_ROOT`;波 2 起该路径由
#: ACS 平台在沙箱启动时建 NAS 挂载 symlink,镜像里绝不能预先建出这个目录
#: (``Dockerfile`` 的 ``WORKDIR``/``mkdir`` 两条都会创建目录,已删),``HOME``
#: 因此挪到镜像里另一处早就存在、agent 用户自己拥有的路径——``useradd -m``
#: 建的家目录。落在沙箱本地盘,不随沙箱重建持久(与 :data:`WORKSPACE_ROOT`
#: 的 NAS 语义不同),细节见 ``infra/sandbox-image/Dockerfile`` 头注释与
#: ``docs/superpowers/specs/2026-08-07-sandbox-migration-w2-design.md`` § 二之二。
SANDBOX_HOME = "/home/agent"

#: 沙箱镜像里的 ``agent`` 用户(uid 10000,``nologin``,``useradd -m`` 建的)
#: ——E2B SDK 默认以用户 ``user`` 执行 ``commands.run`` / ``files.write``,那个
#: 账号在我们的镜像里不存在(``AuthenticationException: invalid username:
#: 'user'``,2026-08-04 探针报告实测)。做成常量而非散落字面量 —— Task 8 的
#: ``exec`` 也要用同一个值。
#:
#: **勘误(W2 收尾真栈复跑)**:这里原本还写着"``user="root"`` 同样不行
#: (``InvalidArgumentException``)"。那条是 2026-08-04 在**波 1 老镜像**上测
#: 的,当时镜像声明 ``USER agent``;Task 9 让容器 root 启动之后不再成立 ——
#: ``AgentSandboxClient._chown_workspace_mount``(方向变更前叫
#: ``_chmod_workspace_mount``)就是以 ``user="root"`` 跑,2026-08-07 真栈实测
#: **成功**(挂载共享那条契约用例正是靠它从 PermissionError 转
#: PASSED)。留着那句话会告诉后来者"别用 root",而我们恰恰
#: 靠 root 修好了挂载点权限那条 Critical。
#:
#: 这个常量本身含义不变:它钉的是**执行身份**(降权后跑用户代码的那个用户),
#: 不是容器身份。root 可用于平台自己的一次性运维动作,不是 ``exec`` 的落点。
#:
#: 沙箱迁移 W2 Task 9(2026-08-07)起镜像不再声明 ``USER agent``(容器本身
#: root 启动,理由见 ``infra/sandbox-image/Dockerfile`` 头注释)——这个常量
#: 钉的是执行身份,不是容器身份,含义不变,仍是 ``commands.run``/
#: ``files.write`` 唯一能用的降权用户。
SANDBOX_EXEC_USER = "agent"

#: :data:`SANDBOX_EXEC_USER` 对应的数字 uid —— 与 control-plane 镜像方向变更
#: (共享 gid → 统一 uid,见
#: ``docs/superpowers/specs/2026-08-08-workspace-gid-sharing-design.md`` § 六)
#: 之后共用的同一个数字。**这是两份 Dockerfile 各自 ``useradd`` 行的字面量
#: 副本,不是从任一份 Dockerfile 解析出来的**——原因与上面 ``SANDBOX_IMAGE_ENV``
#: 一样:构建期声明,运行时读不到。真正需要与 Dockerfile 保持一致的调用点是
#: ``AgentSandboxClient._chown_workspace_mount`` 的兜底 ``chown``(那句以
#: ``user="root"`` 跑,只有 root 才能真正改属主,mode 收紧到 0700 之后这是
#: 唯一还能把一个属主是 root 的 subPath 目录交给 agent 的路径)——之前那句
#: 直接硬编码 ``"chown 10000:10000"``,与两份 Dockerfile 的 uid 之间没有任何
#: 机器可检的联系:``test_workspace_shared_uid.py`` 只比对两份 Dockerfile
#: 互相是否相等,两边一起改成同一个新数字(比如 10007)时那道闸照样绿,而这
#: 句硬编码的 chown 仍然把目录交给不存在的旧 uid 10000——沙箱进程此时已经是
#: 10007,新目录对它而言又是别人的。把字面量收在这一个常量里、调用点改成引用
#: 它,``test_workspace_shared_uid.py`` 才有东西可比对、可钉住这第三处。
SANDBOX_EXEC_UID = 10000

#: 沙箱镜像 ``infra/sandbox-image/Dockerfile`` 声明的那套 ``ENV``,在这里重述
#: 一份显式送进云沙箱。
#:
#: **为什么需要**:envd 派生的进程**不继承镜像的 ``ENV``/``WORKDIR``**。
#: 2026-08-04 集群探针实测(不是推断):``cwd`` 与 ``HOME`` 都是
#: ``/home/agent``(镜像是 ``/workspace``),``PIP_USER``/``LANG``/
#: ``MPLCONFIGDIR`` 一律 ``None``。缺了它们:只读 rootfs 上 ``pip install``
#: 必失败(``PIP_USER=1`` 正是为此而设)、matplotlib 没有可写配置目录、中文
#: 输出有编码风险、用户级安装落在工作区外。本地 supervisor 白拿这一切
#: (``runner.py`` 是容器 PID 1、``subprocess.run`` 直接继承容器环境),所以
#: 这是云后端独有的缺口。
#:
#: **单一事实源为什么落在这里**:Dockerfile 的 ``ENV`` 是构建期声明,编排
#: 进程运行时读不到(镜像不在本进程,也不该为几个常量去拉镜像元数据)——
#: "共享一个变量"物理上做不到,只能是两份副本 + 一道对齐闸。这份是唯一真会
#: 被送进云沙箱的副本;``test_image_env_matches_dockerfile`` 解析 Dockerfile
#: 的 ``ENV``/``WORKDIR`` 双向比对,任一边单方面改动都会红(手法同
#: ``test_idle_ttl_matches_supervisor_default``)。
#:
#: 只在 ``create(envs=...)`` 送一次:``connect`` 没有 ``envs`` 形参(e2b
#: 2.24.0),热会话重连不重发 —— 这 7 项全是与实例无关的常量,建时定死即可
#: (与带 per-sandbox token 的 egress 变量不同,见
#: ``AgentSandboxClient._egress_env``)。
SANDBOX_IMAGE_ENV = {
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONUNBUFFERED": "1",
    "HOME": SANDBOX_HOME,
    "MPLCONFIGDIR": f"{SANDBOX_HOME}/.mplconfig",
    "LANG": "zh_CN.UTF-8",
    "LC_ALL": "zh_CN.UTF-8",
    "PIP_USER": "1",
}

#: 契约常量 —— 云沙箱与本地 supervisor 对同一次 exec 请求要给出等价的
#: clamp/truncate 行为。三个值各自钉的对家不同,别一概而论:
#:
#: * ``DEFAULT_TIMEOUT_S`` —— 三方对齐(supervisor 的
#:   ``SandboxSupervisorSettings.default_timeout_s`` + ``runner.py:28-37``)
#: * ``MAX_TIMEOUT_S`` —— 主钉 supervisor 的 **HTTP 边界**
#:   ``schemas.ExecRequest.timeout_s`` 的 ``le``:超范围的 ``timeout_s`` 在
#:   schema 就被 422 掉,``runner.py`` 那个 clamp 在 HTTP 路径上根本走不到
#:   (全分支终审复审发现)。runner.py 侧的同名常量此前没闸钉着,PR-C #9 起
#:   也一并钉住(防哪天 supervisor 改成直调时静默分叉)
#: * ``MAX_OUTPUT_CHARS`` —— 钉 ``runner.py:28-37``(截断真发生在那里)
#:
#: 对应的闸都在 ``test_exec_contract_constants_match_the_sandbox_image``。
DEFAULT_TIMEOUT_S = 30
MAX_TIMEOUT_S = 300
MAX_OUTPUT_CHARS = 1_000_000

#: 解释器旗标 —— 两后端 exec 子进程共用(PR-C)。刻意是 ``-E -P`` 而非
#: ``-I``:``-I`` 隐含 ``-s``,把 user site 踢出 ``sys.path``,静默弄坏镜像
#: ``PIP_USER=1`` 的按需安装流(装得上、import 不到)。``-E`` 保住 PYTHON*
#: 环境配置隔离(副作用:镜像声明的 PYTHONUNBUFFERED / PYTHONDONTWRITEBYTECODE
#: 对子进程失效 —— 一直如此,记档不修);``-P`` 保住"脚本目录 / cwd 不进
#: sys.path"(防 /tmp、/workspace 落的文件遮蔽 stdlib)。对家是 ``runner.py``
#: 的 subprocess argv,闸在 test_exec_contract_constants_match_the_sandbox_image。
SANDBOX_PYTHON_FLAGS: tuple[str, ...] = ("-E", "-P")
