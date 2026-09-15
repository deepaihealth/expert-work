"""B-61 §4.1 —— ``inputs.json`` 的构造与改写,全是纯函数,不碰 IO。

本轮声明变量落成 agent 目录下的一份 JSON,沙箱代码从 ``$EXPERT_WORK_INPUTS``
读它,不再从提示词里手抄长串(spec §一 的 ``org_logo`` 三轮三错)。

两条结构性决定写在这里,别在调用方重新发明:

* **每个变量统一是对象** ``{"value": …, "trusted": …}``,``value`` 原样保留调用方
  给的结构。统一形状让沙箱代码不用先判类型。
* **``local_path`` 就地挂在 URL 旁边** —— 顶层 URL 变量挂在变量对象上,嵌套的挂在
  那一项上(``materials[0].local_path``)。路径**相对 ``/workspace``**:B-60 之后
  exec 的 cwd 就是 ``/workspace``(即 :data:`sandbox_image_contract.EXEC_VIEW`),
  相对路径与 ``Path("/workspace") / rel`` 都成立。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from expert_work.protocol import PromptVariableSpec
from orchestrator.tools.sandbox_image_contract import EXEC_VIEW

#: 文件名。目录按 run 分(spec §十二:同用户同 agent 并发 run 彻底隔离)。
INPUTS_FILENAME = "inputs.json"


def inputs_rel_dir(run_id: UUID) -> str:
    """本轮 inputs 目录,相对 exec 视图根。"""
    return f"inputs/{run_id}"


def inputs_rel_path(run_id: UUID) -> str:
    """本轮 ``inputs.json``,相对 exec 视图根。"""
    return f"{inputs_rel_dir(run_id)}/{INPUTS_FILENAME}"


def inputs_abs_path(run_id: UUID) -> str:
    """本轮 ``inputs.json`` 在沙箱里的绝对路径(``EXPERT_WORK_INPUTS`` 的值)。"""
    return f"{EXEC_VIEW}/{inputs_rel_path(run_id)}"


@dataclass(frozen=True)
class UrlSite:
    """文档里一个 URL 的位置。

    ``path`` 是从变量的 ``value`` 往下的路径(键或下标),空元组表示 ``value``
    本身就是那个 URL。
    """

    var_name: str
    path: tuple[str | int, ...]
    url: str


def _is_http_url(value: Any) -> bool:
    return isinstance(value, str) and (value.startswith("http://") or value.startswith("https://"))


def build_inputs_doc(
    *,
    run_id: UUID,
    variables: Sequence[PromptVariableSpec],
    inputs: Mapping[str, Any],
) -> dict[str, Any] | None:
    """按声明变量构造文档;没有声明变量返回 ``None``(调用方据此完全跳过)。

    只收**声明过**的变量 —— ``inputs`` 里多出来的键在 control-plane 的
    ``validate_prompt_inputs`` 就已经被拒,这里再挡一次是为了本函数自身可独立推理。
    没传的可选变量**不出现**(不是 null),让沙箱代码用 ``in`` 判断即可。
    """
    if not variables:
        return None
    doc_vars: dict[str, Any] = {}
    for spec in variables:
        if spec.name not in inputs:
            continue
        value = inputs[spec.name]
        # 早失败:不可 JSON 序列化的值不该走到写文件那一步再炸。
        json.dumps(value, ensure_ascii=False)
        doc_vars[spec.name] = {"value": value, "trusted": spec.trusted}
    return {"run_id": str(run_id), "variables": doc_vars}


def _walk(value: Any, prefix: tuple[str | int, ...]) -> list[tuple[tuple[str | int, ...], str]]:
    if _is_http_url(value):
        return [(prefix, value)]
    if isinstance(value, Mapping):
        found: list[tuple[tuple[str | int, ...], str]] = []
        for key, item in value.items():
            # inputs 是第三方调用方直接传的 JSON,调用方可能自己就塞了一个叫
            # local_path 的字段(值可以是任意字符串,包括 URL);不挡住它会被
            # 当成待预拉的 site,预拉后又被平台自己的 local_path 覆盖——等于把
            # 调用方指定的地址喂给沙箱的出网请求。这里挡的是租户输入,不是只挡
            # 本模块自己回填的值,删掉前先看
            # test_local_path_key_supplied_by_caller_is_not_a_url_site。
            if key == "local_path":
                continue
            found.extend(_walk(item, (*prefix, str(key))))
        return found
    if isinstance(value, list):
        found = []
        for index, item in enumerate(value):
            found.extend(_walk(item, (*prefix, index)))
        return found
    return []


def iter_url_sites(doc: Mapping[str, Any]) -> list[UrlSite]:
    """文档里所有 http(s) URL 的位置,按变量声明顺序、深度优先。"""
    sites: list[UrlSite] = []
    for name, entry in doc.get("variables", {}).items():
        for path, url in _walk(entry.get("value"), ()):
            sites.append(UrlSite(var_name=name, path=path, url=url))
    return sites


def _set_in(container: Any, path: tuple[str | int, ...], key: str, value: Any) -> Any:
    """沿 ``path`` 深拷贝并在末端容器上写 ``key``(不可变:返回新对象)。"""
    if not path:
        if isinstance(container, Mapping):
            return {**container, key: value}
        msg = f"cannot set {key!r} on {type(container).__name__}"
        raise TypeError(msg)
    head, rest = path[0], path[1:]
    if isinstance(head, int) and isinstance(container, list):
        copied = list(container)
        copied[head] = _set_in(copied[head], rest, key, value)
        return copied
    if isinstance(container, Mapping):
        return {**container, head: _set_in(container[head], rest, key, value)}
    msg = f"path segment {head!r} does not match {type(container).__name__}"
    raise TypeError(msg)


def with_local_path(doc: Mapping[str, Any], site: UrlSite, rel: str | None) -> dict[str, Any]:
    """返回一份新文档,在 ``site`` 旁边写上 ``local_path``(``None`` 写成显式 null)。

    顶层 URL(``site.path`` 为空)挂在变量对象上,嵌套的挂在那一项上。
    """
    variables = doc["variables"]
    entry = variables[site.var_name]
    if not site.path:
        new_entry = {**entry, "local_path": rel}
    else:
        parent = site.path[:-1]
        new_value = _set_in(entry["value"], parent, "local_path", rel)
        new_entry = {**entry, "value": new_value}
    return {**doc, "variables": {**variables, site.var_name: new_entry}}
