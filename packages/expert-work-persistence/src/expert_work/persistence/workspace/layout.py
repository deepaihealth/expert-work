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

import re

#: Activated skill packages, seeded at ``skills/<name>/…`` (skill-runtime §5.1).
#: Sandbox migration wave 2 (spec § 四) moved the actual seed *destination*
#: out from under this workspace prefix to :data:`SANDBOX_SKILLS_ROOT` — this
#: constant (and :data:`WORKSPACE_RESERVED_PREFIXES` below) stays only to keep
#: hiding any pre-wave-2 workspace residue from the browse view.
WORKSPACE_SKILLS_DIR = "skills"

#: User-uploaded documents, landing at ``uploads/<name>`` for ``read_document``.
WORKSPACE_UPLOADS_DIR = "uploads"

#: 注入变量的落点(B-61):``inputs/<run_id>/inputs.json`` 是本轮声明变量的文档,
#: ``inputs/cache/<sha256(url)[:32]><ext>`` 是按 agent 共享的内容寻址预拉缓存。
#:
#: 两者都是**平台自己派生的机械产物**,不是 agent 产出:文档由 run 启动时的 inputs
#: 节点写,缓存条目是平台代模型下载的副本(重下即得)。所以它和 ``uploads`` /
#: ``skills`` / ``.tool_results`` 一样进保留前缀 —— 不进的话,用户的「产物」列表里
#: 会混进每一个不透明的 ``<32 位 hex>.png`` 和每一轮的 ``inputs.json``。
#:
#: **不进** :data:`WORKSPACE_DELETE_PROTECTED_PREFIXES`:与 ``.tool_results`` 同一个
#: 理由 —— 它是平台**要能自己回收**的垃圾(control-plane 的 workspace janitor 按 TTL
#: 收它),而删除保护挡的是「客户端不许删平台写的东西」。这里两件事不同向。
WORKSPACE_INPUTS_DIR = "inputs"

#: Container for the per-agent subtrees — ``agents/<agent_key>/…`` (B-50).
#: A user's volume is mounted per ``(tenant, user)`` and cannot be split by
#: agent at the mount point (hot sandboxes are reused across agents, the CSI
#: ``subPath`` is fixed at create time — spec § 三), so the split is done in
#: the path and enforced at the tool boundary.
WORKSPACE_AGENTS_DIR = "agents"

#: Legacy whose owning agent could not be inferred at migration time (B-50
#: § 7.1). Readable from any agent via the explicit ``shared:`` prefix,
#: never writable — it is frozen on migration day and decays with time.
WORKSPACE_SHARED_DIR = "shared"

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

#: Tool-result overflow cache — ``.tool_results/<run_id>/…``. Pure machinery:
#: a run externalises an over-long tool result here and references it back by
#: path. Nothing a person browsing the workspace wants to see.
#:
#: Same value as ``orchestrator.tools.overflow.OVERFLOW_DIR``; that module
#: imports this one rather than keeping a second literal.
WORKSPACE_OVERFLOW_DIR = ".tool_results"

#: ``read_page`` 渲出来的页在工作区里的落点形状(B-64 回修第 5 轮 I-1)。完整形状:
#:
#:     ``.tool_results/<run_id>/figures/<doc-sha>/<render-sha>/_u<unit>/page-NN.jpg``
#:
#: 这几个常量放在这个模块,理由与本模块 docstring 开头说的完全一样:**写它的人和
#: 读它的人必须不能各写一份**。写的是 ``orchestrator.tools.read_page``(宿主拼前四
#: 段、沙箱片段拼后三段,片段那几段也是从这里当参数传进去的,不是第二份字面量);
#: 读的是 ``orchestrator.multimodal.is_cacheable_image_ref`` —— 它要判「这条 ref 指
#: 的是不是一张**内容派生**的渲染页」,因为只有那种落点缓存才不会发旧字节。
#:
#: 判据之所以必须认**形状**而不是认 ``.tool_results/`` 这个目录前缀:那个目录不是
#: 写保护的,模型自己就能往里写(``write_file`` 对 ``.tool_results/evil.jpg`` 放行),
#: 再把它交给 ``ask_image``——一个目录前缀通行证等于把「可缓存」发给了模型自己写的、
#: 随时会被覆盖的文件。
RENDERED_FIGURE_DIR = "figures"
#: 两段哈希(``<doc-sha>`` 路径派生、``<render-sha>`` 渲染输入派生)各取 sha256
#: 十六进制的前多少位。
RENDERED_FIGURE_SHA_HEX_LEN = 16
#: 每个 unit 自己的私有产出子目录前缀:``_u<unit>``。
RENDERED_FIGURE_UNIT_PREFIX = "_u"
#: 交给 pdftoppm 的文件名主干;pdftoppm 自己补上 ``-<页号>.jpg``,页号补零到**总页数**
#: 的宽度,所以这里只能认「``-`` 加若干位数字」,不能认固定位宽。
RENDERED_FIGURE_PAGE_STEM = "page"

_UUID_PATTERN = "[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_SHA_PATTERN = f"[0-9a-f]{{{RENDERED_FIGURE_SHA_HEX_LEN}}}"
_RENDERED_FIGURE_RE = re.compile(
    "^"
    + re.escape(WORKSPACE_OVERFLOW_DIR)
    + "/"
    + _UUID_PATTERN
    + "/"
    + re.escape(RENDERED_FIGURE_DIR)
    + "/"
    + _SHA_PATTERN
    + "/"
    + _SHA_PATTERN
    + "/"
    + re.escape(RENDERED_FIGURE_UNIT_PREFIX)
    + "[1-9][0-9]*/"
    + re.escape(RENDERED_FIGURE_PAGE_STEM)
    + r"-[0-9]+\.jpg$"
)


def is_rendered_figure_rel(rel: str) -> bool:
    """``rel`` 是不是一条 ``read_page`` 渲染页的落点(见上面那组常量)。

    ``rel`` 是**用户根相对**路径,且调用方需要先把 ``agents/<key>/`` 这类作用域前缀
    剥掉(``is_cacheable_image_ref`` 就是这么做的)。

    严格是**故意**的,而且失败方向是安全那一侧:判错成 ``False`` 只是让一条本可以
    缓存的 ref 每轮多读一次盘(慢),判错成 ``True`` 才是发旧字节(错)。所以宁可
    严。代价是「严到把功能关掉了还不知道」——
    ``test_a_real_read_page_ref_is_recognised_end_to_end`` 用宿主 + 片段真拼出来的
    路径钉住了这一侧,不是靠手写一条样本路径。
    """
    return _RENDERED_FIGURE_RE.match(rel) is not None


#: Workspace prefixes that hold machinery / inputs rather than agent output —
#: hidden from the "agent products" browse view. Add a new reserved namespace
#: here (and use the matching constant where it is written) and every browse
#: surface picks it up automatically.
WORKSPACE_RESERVED_PREFIXES: frozenset[str] = frozenset(
    {WORKSPACE_SKILLS_DIR, WORKSPACE_UPLOADS_DIR, WORKSPACE_OVERFLOW_DIR, WORKSPACE_INPUTS_DIR}
)

#: Prefixes a *client* may never delete — the browse-hidden set minus the
#: overflow cache.
#:
#: Hiding and delete-protection used to be the same set, which was right while
#: it held only ``skills`` (seeded machinery) and ``uploads`` (user input):
#: both are written by the platform and must survive whatever a client asks
#: for. ``.tool_results`` breaks that tie — it is equally uninteresting to a
#: browsing human, but it is *garbage* the platform itself has to be able to
#: collect: the session-purge hook rm -rf's ``.tool_results/<run_id>/`` through
#: ``delete_tree``, and a single shared set would make that call raise
#: "path is reserved and cannot be deleted".
#:
#: So the two concerns are now two constants. Reusing one for both is the
#: kind of coupling that only shows up when a new member disagrees with the
#: others — and then it shows up as a feature that silently cannot work.
WORKSPACE_DELETE_PROTECTED_PREFIXES: frozenset[str] = frozenset(
    {WORKSPACE_SKILLS_DIR, WORKSPACE_UPLOADS_DIR}
)

#: Container prefixes that are *positional*, not reserved: they say **whose**
#: subtree follows, not what kind of content it is. They are stripped before
#: the reserved check so the check keeps meaning the same thing at every depth.
#: ``agents`` takes a key segment after it, ``shared`` takes none.
_CONTAINERS: dict[str, int] = {WORKSPACE_AGENTS_DIR: 2, WORKSPACE_SHARED_DIR: 1}


def _head_after_container(relpath: str) -> str:
    """The first path segment once a known container prefix is stripped.

    ``""`` for an empty path. See :func:`is_reserved_workspace_path` for why
    only known containers are stripped and only one segment is examined.
    """
    parts = tuple(p for p in relpath.strip().split("/") if p)
    if not parts:
        return ""
    skip = _CONTAINERS.get(parts[0])
    if skip is not None and len(parts) > skip:
        parts = parts[skip:]
    return parts[0]


def is_reserved_workspace_path(relpath: str) -> bool:
    """Return whether ``relpath`` lives under a reserved (non-output) namespace.

    Historically this compared only the **top** segment, which was the whole
    truth while the workspace was one flat tree. B-50 moved everything under
    ``agents/<agent_key>/`` (and the un-attributable legacy under ``shared/``),
    so a top-segment rule stops matching ``agents/<key>/uploads/x.docx`` —
    and uploaded documents would suddenly appear in the browse view's
    "products" list. That is a *silent* behaviour change introduced by the
    migration: nothing errors, the list just grows files that were never
    agent output.

    So: strip a known container prefix, then look at the first segment of
    what remains. Only **known** containers are stripped and only the first
    segment is checked — deliberately not "any segment named uploads",
    which would swallow an agent's own ``客户案例/uploads/`` directory.

    A bare top-level file (no ``/``) is never reserved, and neither is a
    bare container directory with nothing under it.
    """
    return _head_after_container(relpath) in WORKSPACE_RESERVED_PREFIXES


def is_delete_protected_workspace_path(relpath: str) -> bool:
    """Whether a client is forbidden from deleting ``relpath``.

    A strict subset of :func:`is_reserved_workspace_path` — see
    :data:`WORKSPACE_DELETE_PROTECTED_PREFIXES` for why the two are not the
    same set. Callers guarding a delete endpoint want **this** one; callers
    building a browse listing want the other.
    """
    return _head_after_container(relpath) in WORKSPACE_DELETE_PROTECTED_PREFIXES
