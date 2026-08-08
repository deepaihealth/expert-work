"""Workspace volume layout conventions — Stream J.15.

A user's ``/workspace`` mixes three kinds of content in one flat volume:

* **agent output** — whatever the agent wrote (a generated PDF, ``out/…``);
  this is what a user actually wants to retrieve.
* **machinery** — activated skill packages the runtime seeds before exec
  (skill-runtime §5.1), materialised under ``skills/<name>/…``.
* **inputs** — documents the user uploaded for a later run's ``read_document``,
  landing under ``uploads/…``.

The machinery + input namespaces are *system-reserved*: the platform controls
exactly where they go, so they can be enumerated. Agent output, by contrast,
can be written anywhere and cannot be allow-listed. The browse / download
surface therefore **hides the reserved prefixes and shows everything else** —
the same model as ``.gitignore`` hiding generated dirs.

This module is the single source of truth for those prefixes: the seeders that
*write* them and the browser that *hides* them both import from here, so a path
change in one place can never silently desync from the filter.
"""

from __future__ import annotations

#: Activated skill packages, seeded at ``skills/<name>/…`` (skill-runtime §5.1).
#: Sandbox migration wave 2 (spec § 四) moved the actual seed *destination*
#: out from under this workspace prefix to :data:`SANDBOX_SKILLS_ROOT` — this
#: constant (and :data:`WORKSPACE_RESERVED_PREFIXES` below) stays only to keep
#: hiding any pre-wave-2 workspace residue from the browse view.
WORKSPACE_SKILLS_DIR = "skills"

#: User-uploaded documents, landing at ``uploads/<name>`` for ``read_document``.
WORKSPACE_UPLOADS_DIR = "uploads"

#: Sandbox-local seed root for an agent's activated skill files (sandbox
#: migration wave 2, spec § 四 — "技能方案"). Materialized at
#: ``{SANDBOX_SKILLS_ROOT}/<agent_key>/<skill-name>/…`` on every sandbox
#: acquire, per-agent namespaced so two agents sharing one warm sandbox never
#: clobber each other's skill files. Both backends seed here (the cloud
#: ``AgentSandboxClient`` and the local ``sandbox-supervisor``, Task 6) —
#: sandbox-local disk, not the user's NAS-backed ``/workspace``, so it never
#: occupies workspace quota or shows up in the workspace browse surface.
SANDBOX_SKILLS_ROOT = "/opt/skills"

#: Sandbox-local ``PYTHONUSERBASE`` root, per agent (spec 决策 10). Same
#: sharing problem as skills: two agents on one warm sandbox share
#: ``$HOME/.local`` by default, so a ``pip install --user`` from one can
#: clobber or race the other's. Injected as
#: ``PYTHONUSERBASE={SANDBOX_AGENTS_ROOT}/<agent_key>`` on every ``exec`` call
#: (not just acquire — see ``orchestrator.tools.sandbox.agent_key_envs``).
#: The directory is pip's to create; nothing pre-creates or chowns it beyond
#: the image-level ``/opt/agents`` (sandbox migration wave 2 Task 9).
SANDBOX_AGENTS_ROOT = "/opt/agents"

#: Top-level workspace prefixes that hold machinery / inputs rather than agent
#: output — hidden from the "agent products" browse view. Add a new reserved
#: namespace here (and use the matching constant where it is written) and every
#: browse surface picks it up automatically.
WORKSPACE_RESERVED_PREFIXES: frozenset[str] = frozenset(
    {WORKSPACE_SKILLS_DIR, WORKSPACE_UPLOADS_DIR}
)


def is_reserved_workspace_path(relpath: str) -> bool:
    """Return whether ``relpath`` lives under a reserved (non-output) namespace.

    Compares the first path segment against :data:`WORKSPACE_RESERVED_PREFIXES`;
    a bare top-level file (no ``/``) is never reserved.
    """
    return relpath.split("/", 1)[0] in WORKSPACE_RESERVED_PREFIXES


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
#:
#: ``orchestrator.tools.nas_workspace_store`` 里曾经有一个本地
#: ``_LEAF_FILE_MODE = 0o644``(``rw-r--r--``,``other`` 可读的旧值)——Task 3
#: 把它删了,改成直接引用这个常量。那个旧值能凑效纯粹是因为 ``other`` 位开
#: 着兜底,不是因为 gid 设计对了;换成这个常量后 leaf 文件的可读性由 group
#: 位(配合上面 ``WORKSPACE_DIR_MODE`` 的 setgid)负责,不再靠 ``other`` 放水。
WORKSPACE_FILE_MODE = 0o640
