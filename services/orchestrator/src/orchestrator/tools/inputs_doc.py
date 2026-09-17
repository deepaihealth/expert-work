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
* **字符串值若能解析成 JSON 容器,另存一份 ``value_parsed``**(B-67 §4.3):URL 扫描与
  ``local_path`` 回填看它;``value`` 仍是原字符串,契约不变。

**``local_path`` 可能指向一个已经不在的文件 —— 这是被接受的契约,不是 bug。**
预拉缓存由 control-plane 的 workspace janitor 按 7 天回收,而本文档所在的 run 目录保留
30 天(两条保留期刻意不同:缓存是会随轮数涨的那一半,必须有界;文档是几 KB 的 JSON,留久
了才救得了「挂起很久的审批续跑」)。所以一个挂得够久的 run 续跑时,可能拿到一个指向已回收
文件的 ``local_path``。**``value`` 里永远留着原始 URL**,沙箱代码照着重下即可;反过来为了
保住 ``local_path`` 去删 run 目录,模型手里就什么都没有了,只能回去从提示词手抄长串 ——
那正是本项目要消灭的失败。
"""

from __future__ import annotations

import json
import posixpath
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

from expert_work.persistence.workspace.layout import WORKSPACE_INPUTS_DIR
from expert_work.protocol import PromptVariableSpec
from orchestrator.tools.sandbox_image_contract import EXEC_VIEW

#: 文件名。目录按 run 分(spec §十二:同用户同 agent 并发 run 彻底隔离)。
INPUTS_FILENAME = "inputs.json"

#: B-67 §4.3 —— 字符串值尝试当 JSON 解析的上限(UTF-8 字节),与 read_document 的
#: 内联上限同数量级,防 CPU。
MAX_PARSE_BYTES = 64 * 1024
#: 文档里存解析结果的键;``value`` 原样保留(契约不变)。
PARSED_KEY = "value_parsed"
#: 链接名里 slug 取 description 的前多少个字。
SLUG_MAX_CHARS = 40
#: slug 与 dict 键只保留 ``\w`` 与 ``-``(``\w`` 已含 CJK)。这两段都是租户数据,进
#: 文件名前必须净化(``..`` / ``/``);``.`` 也剥,撞名后缀 ``-2`` 才能可靠地插在扩展名
#: 之前。沙箱侧 ``prefetch_script._SLUG_DROP`` 是逐字复制。
_SLUG_DROP = re.compile(r"[^\w-]")
#: 链接名的扩展名:只认 URL 路径后缀,且形状受限。cache 文件名照旧走 ``pick_suffix``。
_EXT_RE = re.compile(r"^\.[A-Za-z0-9]{1,5}$")


def inputs_rel_dir(run_id: UUID) -> str:
    """本轮 inputs 目录,相对 exec 视图根。

    目录名取共享包的 ``WORKSPACE_INPUTS_DIR``,不写字面量:同一个名字还被浏览面的保留
    前缀(``WORKSPACE_RESERVED_PREFIXES``)与 control-plane 的回收闸用着,抄三份就是留
    两条会静默走散的缝。沙箱侧的 ``prefetch_script`` 是唯一的例外 —— 它要能在没有本仓库
    的沙箱里独立运行,只能用 stdlib、只能写字面量(那边有注释指回这里)。
    """
    return f"{WORKSPACE_INPUTS_DIR}/{run_id}"


def inputs_rel_path(run_id: UUID) -> str:
    """本轮 ``inputs.json``,相对 exec 视图根。"""
    return f"{inputs_rel_dir(run_id)}/{INPUTS_FILENAME}"


def inputs_abs_path(run_id: UUID) -> str:
    """本轮 ``inputs.json`` 在沙箱里的绝对路径(``EXPERT_WORK_INPUTS`` 的值)。"""
    return f"{EXEC_VIEW}/{inputs_rel_path(run_id)}"


def inputs_abs_dir(run_id: UUID) -> str:
    """本轮 inputs 目录在沙箱里的绝对路径(``EXPERT_WORK_INPUTS_DIR`` 的值)。"""
    return f"{EXEC_VIEW}/{inputs_rel_dir(run_id)}"


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


def parse_json_value(value: Any) -> list[Any] | dict[str, Any] | None:
    """字符串能 ``json.loads`` 成 list / dict 就返回解析结果,否则 ``None``(§4.3)。

    只试以 ``[`` / ``{`` 开头、不超过 :data:`MAX_PARSE_BYTES` 的字符串;非字符串、标量
    JSON(``"42"``)、解析失败都是 ``None``。调用方拿到 ``None`` 就按原值处理。
    """
    if not isinstance(value, str):
        return None
    if value.lstrip()[:1] not in ("[", "{") or len(value.encode("utf-8")) > MAX_PARSE_BYTES:
        return None
    try:
        parsed = json.loads(value)
    except ValueError:
        return None
    return parsed if isinstance(parsed, list | dict) else None


def root_key(entry: Mapping[str, Any]) -> str:
    """URL 扫描与 ``local_path`` 回填看哪一份:有 ``value_parsed`` 看它,否则 ``value``。"""
    return PARSED_KEY if PARSED_KEY in entry else "value"


def url_suffix(url: str) -> str:
    """链接名的扩展名:URL 路径后缀,匹配 ``_EXT_RE`` 才要。渲染层在预拉之前就要说出
    名字,它手里只有 URL —— 所以这里**不看** Content-Type。"""
    ext = posixpath.splitext(urlparse(url).path)[1]
    return ext if _EXT_RE.match(ext) else ""


def slugify(text: str | None) -> str:
    """description / dict 键 → 文件名片段:取前 :data:`SLUG_MAX_CHARS` 字,只留 ``[\\w-]``。"""
    if not text:
        return ""
    return _SLUG_DROP.sub("", text[:SLUG_MAX_CHARS])


def site_description(root: Any, path: Sequence[str | int]) -> str | None:
    """列表项的说明:沿 ``path`` 走到第一个下标那一层,取该项的 ``description``(字符串才算)。"""
    cursor = root
    for step in path:
        if isinstance(step, int):
            item = cursor[step] if isinstance(cursor, list) and 0 <= step < len(cursor) else None
            desc = item.get("description") if isinstance(item, Mapping) else None
            return desc if isinstance(desc, str) else None
        if not isinstance(cursor, Mapping):
            return None
        cursor = cursor.get(step)
    return None


def _link_stem(var_name: str, path: Sequence[str | int], description: str | None) -> str:
    """不带扩展名的链接名。顶层 ``<var>``;dict 字段 ``<var>.<k1>.<k2>``;遇到第一个下标
    ``i`` 就是列表项:``<var>[.<keys>]/<i>[-<slug>]``,下标之后的键不进名字(一项里两个
    URL 字段会撞名,由 :func:`link_names` 加 ``-2``)。"""
    keys: list[str] = []
    for step in path:
        if isinstance(step, int):
            head = ".".join([var_name, *keys])
            slug = slugify(description)
            return f"{head}/{step}-{slug}" if slug else f"{head}/{step}"
        keys.append(slugify(str(step)) or "_")
    return ".".join([var_name, *keys])


def link_names(
    var_name: str, sites: Sequence[tuple[Sequence[str | int], str, str | None]]
) -> list[str]:
    """一个变量全部 site 的链接名,按 site 顺序;撞名按出现顺序加 ``-2`` / ``-3``(插在
    扩展名之前)。``sites`` 每项 ``(path, url, description)``。

    **这是链接名的唯一算法**:渲染层(control-plane)、「本轮输入」段、守卫提示、宿主侧
    site 枚举都调它;沙箱脚本 ``prefetch_script._link_names`` 是逐字复制,等价测试钉住。
    """
    seen: dict[str, int] = {}
    out: list[str] = []
    for path, url, description in sites:
        stem, ext = _link_stem(var_name, path, description), url_suffix(url)
        count = seen.get(stem + ext, 0) + 1
        seen[stem + ext] = count
        out.append(f"{stem}{ext}" if count == 1 else f"{stem}-{count}{ext}")
    return out


@dataclass(frozen=True)
class LinkedSite:
    """一个 URL site 加上它在 run 目录里的链接名(相对 ``inputs/<run_id>/``)。"""

    site: UrlSite
    link: str


def linked_sites(var_name: str, value: Any) -> list[LinkedSite]:
    """一个变量**原始值**的全部 URL site 及链接名(含 §4.3 的 JSON 字符串形态)。

    渲染层、「本轮输入」段、手抄守卫都从这里取候选 —— 与 inputs.json 里预拉脚本看到的
    site 一字不差(同一个 walker、同一个命名)。
    """
    parsed = parse_json_value(value)
    root = parsed if parsed is not None else value
    found = _walk(root, (), assignable=True)
    names = link_names(var_name, [(path, url, site_description(root, path)) for path, url in found])
    return [
        LinkedSite(site=UrlSite(var_name=var_name, path=path, url=url), link=name)
        for (path, url), name in zip(found, names, strict=True)
    ]


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
        entry: dict[str, Any] = {"value": _null_local_paths(value), "trusted": spec.trusted}
        # B-67 §4.3 —— JSON 字符串(如 materials 契约)解析后另存一份,原 value 不动。
        parsed = parse_json_value(value)
        if parsed is not None:
            entry[PARSED_KEY] = _null_local_paths(parsed)
        doc_vars[spec.name] = entry
    return {"run_id": str(run_id), "variables": doc_vars}


def _null_local_paths(value: Any) -> Any:
    """把调用方自带的 ``local_path`` 一律清成 ``null``(不可变:返回新对象)。

    工具描述对模型的承诺是「``local_path`` 非空 = 平台已经把文件下载到本地」。
    调用方可以在自己的 JSON 里塞一个 ``local_path``(值可以是任意字符串,包括一个
    URL),两个 walker 都拒绝去**预拉**这个键,但值仍然留在文档里,于是那句承诺就
    成了谎话。构造文档时就抹平:键保留(形状不变)、值清空,真命中时由沙箱里的预拉
    脚本写回真实的相对路径。
    """
    if isinstance(value, Mapping):
        return {
            key: (None if key == "local_path" else _null_local_paths(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_null_local_paths(item) for item in value]
    return value


def _walk(
    value: Any, prefix: tuple[str | int, ...], *, assignable: bool
) -> list[tuple[tuple[str | int, ...], str]]:
    """``assignable`` = 这一层的 URL 旁边有没有地方记 ``local_path``。

    只有两种位置记得下:变量**整个** ``value``(记在变量对象上),或某个 Mapping 的
    一个键(记成同级的 ``local_path``)。列表里的**裸字符串**没有 ——
    ``{"images": ["https://a"]}`` 的第 0 项要挂 ``local_path`` 只能改写字符串自己,
    那既不是文档的形状、也没法表达。这类 URL 因此不算 site:平台不预拉,模型照旧自
    己下载(降级,不是坏掉)。沙箱侧 ``prefetch_script._sites`` 必须同义 —— 那边的
    ``_assign`` 会直接 ``TypeError``,一条这样的 URL 足以把整轮预拉的结果全带走
    (见 ``test_site_walk_matches_the_host_side_implementation``)。
    """
    if _is_http_url(value):
        return [(prefix, value)] if assignable else []
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
            found.extend(_walk(item, (*prefix, str(key)), assignable=True))
        return found
    if isinstance(value, list):
        found = []
        for index, item in enumerate(value):
            found.extend(_walk(item, (*prefix, index), assignable=False))
        return found
    return []


def iter_url_sites(doc: Mapping[str, Any]) -> list[UrlSite]:
    """文档里所有 http(s) URL 的位置,按变量声明顺序、深度优先。"""
    sites: list[UrlSite] = []
    for name, entry in doc.get("variables", {}).items():
        for path, url in _walk(entry.get(root_key(entry)), (), assignable=True):
            sites.append(UrlSite(var_name=name, path=path, url=url))
    return sites
