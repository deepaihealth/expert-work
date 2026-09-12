"""Runbook 让在 pod 里跑的 ``python -m tools.<pkg>``,镜像必须真装了那个包。

2026-09-12 实测:`workspace-agent-scoping-migration.md` 的 Step 1/2 让在
control-plane pod 里跑 ``python3 -m tools.persistence.migrate_workspace_agent_scoping``,
而当时的 Dockerfile 只 ``COPY tools/eval`` —— pod 里 import 直接
``ModuleNotFoundError: No module named 'tools.persistence'``。写 runbook 的人
(我)在本机仓库根目录试的命令,那里 ``tools/`` 当然在;镜像里不在。

**这条测试防的不是「脚本写错了」,是「文档写的命令在它真正要跑的地方跑不起来」**
—— 而那个地方是生产发布当晚,不是开发机(同 [[description-is-not-the-thing]]:
runbook 是对操作的描述,不是操作本身)。
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNBOOKS = REPO_ROOT / "docs" / "runbooks"
CONTROL_PLANE_DOCKERFILE = REPO_ROOT / "services" / "control-plane" / "Dockerfile"

_FENCE = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)
_MODULE = re.compile(r"python3?\s+-m\s+tools\.([a-z_][a-z0-9_]*)")
_COPY = re.compile(r"^COPY\s+(?:--\S+\s+)*tools/([a-z_][a-z0-9_]*)\s", re.MULTILINE)


def _packages_the_image_ships() -> set[str]:
    return set(_COPY.findall(CONTROL_PLANE_DOCKERFILE.read_text()))


def _packages_runbooks_invoke_inside_a_pod() -> dict[str, set[str]]:
    """{tools 子包 -> 引用它的 runbook 文件名}。

    只算**同一个代码块里出现 ``kubectl`` + ``exec``** 的调用 —— 那是「这条命令
    在 pod 里跑」的判据。裸 ``python -m tools.…``(例如 volume-restore.md)是在
    开发机跑的,镜像装不装无关。
    """
    found: dict[str, set[str]] = {}
    for md in sorted(RUNBOOKS.glob("*.md")):
        for block in _FENCE.findall(md.read_text()):
            if "kubectl" not in block or "exec" not in block:
                continue
            for pkg in _MODULE.findall(block):
                found.setdefault(pkg, set()).add(md.name)
    return found


def test_every_in_pod_runbook_module_ships_in_the_control_plane_image() -> None:
    required = _packages_runbooks_invoke_inside_a_pod()
    shipped = _packages_the_image_ships()

    missing = {pkg: sorted(docs) for pkg, docs in required.items() if pkg not in shipped}

    assert not missing, (
        f"runbook 让在 pod 里跑 `python -m tools.<pkg>`,但 control-plane 镜像没装:{missing}。"
        f" 镜像目前装了 {sorted(shipped)}。"
        " 要么给 Dockerfile 补一行 COPY tools/<pkg> /app/tools/<pkg>,"
        " 要么把 runbook 改成不依赖 pod 内的那个模块 —— 别留着一条发布当晚才炸的命令。"
    )


def test_the_guard_itself_has_something_to_guard() -> None:
    """左集合非空自证 —— 没有任何 in-pod 调用时上面那条恒真。

    见 [[verify-where-it-can-fail]]:先确认要遍历的东西真的存在,再看断言结果。
    """
    required = _packages_runbooks_invoke_inside_a_pod()
    assert required, (
        "docs/runbooks/ 里一条 `kubectl exec … python -m tools.<pkg>` 都没扫到 —— "
        "要么正则跟文档写法漂了,要么文档真的不再这么用。两种情况都得看一眼,"
        "别让上面那条断言在空集合上恒真。"
    )
    assert "persistence" in required, (
        "扫不到 tools.persistence —— 工作区搬迁 runbook 的 Step 1/2 正是靠它,"
        f"当前扫到的是 {sorted(required)}。"
    )


# ---------------------------------------------------------------------------
# 同一个失效类的第二种形态:选择器选不中东西。
#
# 2026-09-12 同一份 runbook 的同一段:`-l app=control-plane` 返回空,`exec ""`
# 报 "pod, type/name or --filename must be specified"。本仓库的 Deployment 一律
# 用 `app.kubernetes.io/name`,`app` 这个键根本不存在 —— smoke.sh / release.sh
# 都写对了,只有手写的 runbook 写错。
# ---------------------------------------------------------------------------

BASE = REPO_ROOT / "infra" / "k8s" / "base"

_SELECTOR = re.compile(r"-l\s+([A-Za-z0-9._/-]+)=([A-Za-z0-9._-]+)")


def _labels_the_manifests_declare() -> set[tuple[str, str]]:
    """base 里每个 pod 模板真正带的 label 对。"""
    pairs: set[tuple[str, str]] = set()
    for manifest in BASE.rglob("*.yaml"):
        try:
            docs = list(yaml.safe_load_all(manifest.read_text()))
        except yaml.YAMLError:
            continue  # kustomization 里的 patch 片段等,跳过
        for doc in docs:
            if not isinstance(doc, dict):
                continue
            template = (doc.get("spec") or {}).get("template")
            if not isinstance(template, dict):
                continue
            labels = (template.get("metadata") or {}).get("labels") or {}
            pairs |= {(str(k), str(v)) for k, v in labels.items()}
    return pairs


def _selectors_runbooks_use() -> dict[tuple[str, str], set[str]]:
    found: dict[tuple[str, str], set[str]] = {}
    for md in sorted(RUNBOOKS.glob("*.md")):
        for block in _FENCE.findall(md.read_text()):
            if "kubectl" not in block:
                continue
            for key, value in _SELECTOR.findall(block):
                found.setdefault((key, value), set()).add(md.name)
    return found


def test_every_runbook_label_selector_matches_a_real_pod_label() -> None:
    declared = _labels_the_manifests_declare()
    used = _selectors_runbooks_use()

    # 只判「组件名」这一类选择器 —— value 与 base 下某个组件目录同名的那些。
    # 别的(例如按 job-name 选)不在 base 的 pod 模板里,不是本条要管的。
    components = {d.name for d in BASE.iterdir() if d.is_dir()}
    relevant = {pair: docs for pair, docs in used.items() if pair[1] in components}

    bad = {f"{k}={v}": sorted(docs) for (k, v), docs in relevant.items() if (k, v) not in declared}

    assert not bad, (
        f"runbook 里的 kubectl 选择器选不中任何 pod:{bad}。"
        f" base 的 pod 模板实际带的是 {sorted({k for k, _ in declared})} 这些键"
        " —— 本仓库统一用 `app.kubernetes.io/name`,`-l app=<组件>` 恒返回空,"
        ' 而 `kubectl exec ""` 的报错('
        "pod, type/name or --filename must be specified) 完全看不出是选择器的锅。"
    )


def test_the_selector_guard_has_something_to_guard() -> None:
    """左集合非空自证(同上)。"""
    assert _labels_the_manifests_declare(), "base 下一个 pod 模板 label 都没解析出来"
    components = {d.name for d in BASE.iterdir() if d.is_dir()}
    relevant = [p for p in _selectors_runbooks_use() if p[1] in components]
    assert relevant, (
        "docs/runbooks/ 里一条指向 base 组件的 `kubectl -l <k>=<v>` 都没扫到 —— "
        "正则或文档写法漂了,别让上面那条在空集合上恒真。"
    )
