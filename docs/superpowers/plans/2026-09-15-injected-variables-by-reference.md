# B-61 注入变量按引用 + 工具参数绑定 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 run 的声明变量不再经过模型的「手」—— 沙箱代码从 `inputs.json` 读,MCP 工具参数由平台按配置直接填。

**Architecture:** 两半。**数据面**:图里一个 run-start 节点把本轮声明变量写成 `agents/<key>/inputs/<run_id>/inputs.json`,再在沙箱内跑一段平台自己的脚本把多媒体 URL 预拉成本地文件并回填 `local_path`;`exec_python`/`bash` 通过已有的 per-exec env 通道拿到 `EXPERT_WORK_INPUTS`。**绑定面**:manifest 新增 `arg_bindings`,被绑定的 MCP 参数在建工具目录时从给模型的 JSON schema 里删掉,由 `tools_node` 在审批与 action screening **之前**按本轮 inputs 填回。

**Tech Stack:** Python 3.12 / Pydantic v2 / LangGraph / pytest;前端 React + antd + vitest。

**Spec:** `docs/superpowers/specs/2026-09-15-injected-variables-by-reference-design.md`

## Global Constraints

- **平台默认行为,不是 opt-in 开关**。没有声明变量的 agent 不装 inputs 节点、不写任何文件;有声明变量的 agent 无需任何配置即生效。
- **预拉永远不让 run 失败**。404 / 超时 / 超限 / content-type 不匹配 / 被 agent 出网策略挡住 —— 一律 `local_path` 保持 `null`,run 照跑。
- **预拉不得绕过 `sandbox.network` 策略**:下载必须在沙箱里发生,不得在宿主侧(control-plane / orchestrator pod)发起。
- **不改 run inputs 的对外契约**:键名、64 键 / 单值 8192 字节上限、`required` / `trusted` 语义全部不动。
- **`trusted: false` 的变量,提示词里的围栏内联保持不变**(B-61 不改这部分行为),同时在 `inputs.json` 里给一份。
- **不记值**:日志、审计、指标只记变量名 / 参数名 / 结果 / 字节数,**不记值、不记 URL 全文**。
- **不动 B-60 的 exec 命令串**:`orchestrator/tools/exec_view.py:EXEC_VIEW_SCRIPT` 与 `infra/sandbox-image/runner.py:_EXEC_VIEW_SCRIPT` 那对逐字相同的字面量一个字都不改;env 走 `orchestrator/tools/sandbox.py:96` 的 `agent_key_envs()`。
- **`local_path` 一律相对 `/workspace`**,不是绝对路径。
- **不碰对接方的两个 agent**(`ai-health-plan` / `sop2-designer`):真栈验收只用探针或金丝雀。
- **每条新断言必须变异自证**:break → red → restore → green。修复自带的测试会给坏版本发合格证。
- 本地跑测试用 `uv run --no-sync pytest`;前端用 `pnpm -C apps/admin-ui test`。

---

## 文件结构

| 文件 | 职责 | 归属 |
|---|---|---|
| `services/orchestrator/src/orchestrator/tools/inputs_doc.py`(新) | `inputs.json` 的**纯函数**:构造文档、递归定位 URL、回填 `local_path`、算路径。不做 IO | PR-A |
| `services/orchestrator/src/orchestrator/tools/prefetch_script.py`(新) | 在**沙箱里**运行的预拉脚本。只用 stdlib、自包含(不 import 仓库任何东西),因此本地单测可以直接 import 它测判定逻辑;下发时读自己的源码文本 | PR-A |
| `services/orchestrator/src/orchestrator/graph_builder/inputs_node.py`(新) | run-start 节点:写 `inputs.json` → 沙箱内跑预拉 → 落回。与 `workspace_ingest.py` 同层同形 | PR-A |
| `services/orchestrator/src/orchestrator/tools/sandbox.py`(改 `:96`) | `agent_key_envs()` 增加 `EXPERT_WORK_INPUTS` | PR-A |
| `services/orchestrator/src/orchestrator/agent_factory.py`(改 `:1080` 一带) | 装 inputs 节点的门 | PR-A |
| `services/orchestrator/src/orchestrator/graph_builder/builder.py`(改 `:432`/`:1622`) | 节点接进图 | PR-A |
| `services/orchestrator/src/orchestrator/sse.py`(改 `:381`) | `configurable` 加 `prompt_inputs` 键(A 落,B 用) | PR-A |
| `packages/expert-work-protocol/src/expert_work/protocol/agent_spec.py`(改) | `ArgBindingSpec` + `MCPToolSpec.arg_bindings` + `AgentSpecBody` 跨字段校验 | PR-B |
| `services/orchestrator/src/orchestrator/tools/arg_bindings.py`(新) | 绑定的**纯函数**:剥 schema、填 args。不做 IO | PR-B |
| `services/orchestrator/src/orchestrator/tools/mcp.py`(改 `:944`) | `register_mcp_tools` 接受绑定表并剥 schema | PR-B |
| `services/orchestrator/src/orchestrator/tools/assembly.py`(改 `:688`) | 把 manifest 的绑定传下去 | PR-B |
| `services/orchestrator/src/orchestrator/graph_builder/builder.py`(改 `:1275` 一带) | `tools_node` 填值 + 审计 | PR-B |
| `apps/admin-ui/src/components/manifest-editor/widgets/McpToolPicker.tsx`(改) | 逐工具逐参数「自动 / 绑定」 | PR-C |
| `services/orchestrator/src/orchestrator/tools/prefetch_script.py`(改) | 内容寻址缓存 `inputs/cache/<digest><ext>` | PR-A2 |
| `services/control-plane/src/control_plane/workspace_janitor.py`(改) | 策略表驱动的 TTL 回收 phase | PR-A2 |

---

## Task 1: `inputs_doc.py` —— inputs.json 的纯函数

**Files:**
- Create: `services/orchestrator/src/orchestrator/tools/inputs_doc.py`
- Test: `services/orchestrator/tests/test_inputs_doc.py`

**Interfaces:**
- Consumes: `expert_work.protocol.PromptVariableSpec`(字段 `name` / `trusted` / `required` / `description`)。
- Produces:
  - `INPUTS_FILENAME: str = "inputs.json"`
  - `def inputs_rel_dir(run_id: UUID) -> str` → `"inputs/<run_id>"`
  - `def inputs_rel_path(run_id: UUID) -> str` → `"inputs/<run_id>/inputs.json"`
  - `def inputs_abs_path(run_id: UUID) -> str` → `"/workspace/inputs/<run_id>/inputs.json"`
  - `@dataclass(frozen=True) class UrlSite: var_name: str; path: tuple[str | int, ...]; url: str`
  - `def build_inputs_doc(*, run_id: UUID, variables: Sequence[PromptVariableSpec], inputs: Mapping[str, Any]) -> dict[str, Any] | None`
  - `def iter_url_sites(doc: Mapping[str, Any]) -> list[UrlSite]`
  - `def with_local_path(doc: Mapping[str, Any], site: UrlSite, rel: str | None) -> dict[str, Any]`

> **T1 的历史文本(已交付,#1557)**:本任务正文里的 `inputs/<run_id>/files/<变量名>.<ext>` 路径样例已被 T11 的
> 内容寻址缓存取代(现为 `inputs/cache/<digest><ext>`,见 spec §4.1 勘误),`with_local_path` 在实现收敛时
> 并入 `build_inputs_doc` / `_walk` 一族、未独立留存。保留原文作为当时的决策记录,**不要照它写新代码**。


- [ ] **Step 1: 写失败的测试**

```python
"""B-61 Task 1 —— inputs.json 文档的纯函数。"""

from __future__ import annotations

from uuid import UUID

import pytest
from expert_work.protocol import PromptVariableSpec

from orchestrator.tools.inputs_doc import (
    UrlSite,
    build_inputs_doc,
    inputs_abs_path,
    inputs_rel_path,
    iter_url_sites,
    with_local_path,
)

RUN = UUID("382f6f5a-55c4-49be-ac05-32fa143f010d")


def _var(name: str, *, trusted: bool = True, required: bool = True) -> PromptVariableSpec:
    return PromptVariableSpec(name=name, trusted=trusted, required=required)


def test_paths_are_run_scoped_and_relative_to_the_exec_view() -> None:
    assert inputs_rel_path(RUN) == f"inputs/{RUN}/inputs.json"
    assert inputs_abs_path(RUN) == f"/workspace/inputs/{RUN}/inputs.json"


def test_every_variable_becomes_an_object_carrying_value_and_trusted() -> None:
    doc = build_inputs_doc(
        run_id=RUN,
        variables=[_var("project_code"), _var("notes", trusted=False)],
        inputs={"project_code": "PRJ001", "notes": "客户说…"},
    )
    assert doc is not None
    assert doc["run_id"] == str(RUN)
    assert doc["variables"]["project_code"] == {"value": "PRJ001", "trusted": True}
    assert doc["variables"]["notes"] == {"value": "客户说…", "trusted": False}


def test_no_declared_variables_means_no_document() -> None:
    assert build_inputs_doc(run_id=RUN, variables=[], inputs={}) is None


def test_optional_variable_not_supplied_is_absent_not_null() -> None:
    doc = build_inputs_doc(
        run_id=RUN,
        variables=[_var("a"), _var("b", required=False)],
        inputs={"a": "x"},
    )
    assert doc is not None
    assert "b" not in doc["variables"]


def test_url_sites_are_found_at_the_top_level_and_nested() -> None:
    doc = build_inputs_doc(
        run_id=RUN,
        variables=[_var("org_logo"), _var("materials")],
        inputs={
            "org_logo": "https://example.com/a.jpg",
            "materials": [
                {"description": "视频", "url": "https://example.com/b.mp4"},
                {"description": "无链接"},
            ],
        },
    )
    assert doc is not None
    sites = iter_url_sites(doc)
    assert sites == [
        UrlSite(var_name="org_logo", path=(), url="https://example.com/a.jpg"),
        UrlSite(var_name="materials", path=(0, "url"), url="https://example.com/b.mp4"),
    ]


def test_non_http_strings_are_not_url_sites() -> None:
    doc = build_inputs_doc(
        run_id=RUN,
        variables=[_var("a")],
        inputs={"a": "ftp://example.com/x  and  not-a-url"},
    )
    assert doc is not None
    assert iter_url_sites(doc) == []


def test_with_local_path_is_immutable_and_lands_beside_the_url() -> None:
    doc = build_inputs_doc(
        run_id=RUN,
        variables=[_var("materials")],
        inputs={"materials": [{"description": "视频", "url": "https://example.com/b.mp4"}]},
    )
    assert doc is not None
    site = iter_url_sites(doc)[0]
    updated = with_local_path(doc, site, f"inputs/{RUN}/files/materials.0.mp4")
    assert updated["variables"]["materials"]["value"][0]["local_path"] == (
        f"inputs/{RUN}/files/materials.0.mp4"
    )
    # 原文档没被改(不可变)
    assert "local_path" not in doc["variables"]["materials"]["value"][0]


def test_with_local_path_none_writes_an_explicit_null() -> None:
    doc = build_inputs_doc(
        run_id=RUN, variables=[_var("org_logo")], inputs={"org_logo": "https://example.com/a.jpg"}
    )
    assert doc is not None
    site = iter_url_sites(doc)[0]
    updated = with_local_path(doc, site, None)
    assert updated["variables"]["org_logo"]["local_path"] is None


def test_top_level_url_variable_keeps_value_as_the_url_string() -> None:
    doc = build_inputs_doc(
        run_id=RUN, variables=[_var("org_logo")], inputs={"org_logo": "https://example.com/a.jpg"}
    )
    assert doc is not None
    assert doc["variables"]["org_logo"]["value"] == "https://example.com/a.jpg"


@pytest.mark.parametrize("bad", [{"a": object()}, {"a": {1, 2}}])
def test_non_json_values_are_rejected_loudly(bad: dict[str, object]) -> None:
    with pytest.raises(TypeError):
        build_inputs_doc(run_id=RUN, variables=[_var("a")], inputs=bad)
```

- [ ] **Step 2: 跑测试确认它红**

Run: `uv run --no-sync pytest services/orchestrator/tests/test_inputs_doc.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'orchestrator.tools.inputs_doc'`

- [ ] **Step 3: 写实现**

```python
"""B-61 §4.1 —— ``inputs.json`` 的构造与改写,全是纯函数,不碰 IO。

本轮声明变量落成 agent 目录下的一份 JSON,沙箱代码从 ``$EXPERT_WORK_INPUTS``
读它,不再从提示词里手抄长串(spec §一 的 ``org_logo`` 三轮三错)。

两条结构性决定写在这里,别在调用方重新发明:

* **每个变量统一是对象** ``{"value": …, "trusted": …}``,``value`` 原样保留调用方
  给的结构。统一形状让沙箱代码不用先判类型。
* **``local_path`` 就地挂在 URL 旁边** —— 顶层 URL 变量挂在变量对象上,嵌套的挂在
  那一项上(``materials[0].local_path``)。路径**相对 ``/workspace``**:B-60 之后
  exec 的 cwd 就是 ``/workspace``,相对路径与 ``Path("/workspace") / rel`` 都成立。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from expert_work.protocol import PromptVariableSpec

#: 文件名。目录按 run 分(spec §十二:同用户同 agent 并发 run 彻底隔离)。
INPUTS_FILENAME = "inputs.json"

#: exec 视图根。B-60 之后 ``/workspace`` 就是 ``agents/<key>``。
_EXEC_VIEW = "/workspace"


def inputs_rel_dir(run_id: UUID) -> str:
    """本轮 inputs 目录,相对 exec 视图根。"""
    return f"inputs/{run_id}"


def inputs_rel_path(run_id: UUID) -> str:
    """本轮 ``inputs.json``,相对 exec 视图根。"""
    return f"{inputs_rel_dir(run_id)}/{INPUTS_FILENAME}"


def inputs_abs_path(run_id: UUID) -> str:
    """本轮 ``inputs.json`` 在沙箱里的绝对路径(``EXPERT_WORK_INPUTS`` 的值)。"""
    return f"{_EXEC_VIEW}/{inputs_rel_path(run_id)}"


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
```

- [ ] **Step 4: 跑测试确认它绿**

Run: `uv run --no-sync pytest services/orchestrator/tests/test_inputs_doc.py -v`
Expected: PASS(11 项)

- [ ] **Step 5: 变异自证**

把 `build_inputs_doc` 里的 `"trusted": spec.trusted` 改成 `"trusted": True`,重跑 —— `test_every_variable_becomes_an_object_carrying_value_and_trusted` 必须红。改回,重跑变绿。把 `_walk` 里 `if key == "local_path": continue` 删掉,重跑 —— `test_with_local_path_is_immutable_and_lands_beside_the_url` 后接 `iter_url_sites` 的用例不受影响,所以**另加**一条断言:对已回填过的文档再 `iter_url_sites`,结果与回填前相同。加完再跑一次变异。

- [ ] **Step 6: 提交**

```bash
git add services/orchestrator/src/orchestrator/tools/inputs_doc.py services/orchestrator/tests/test_inputs_doc.py
git commit -m "feat(inputs): inputs.json 的纯函数——构造、定位 URL、回填 local_path(B-61 T1)"
```

---

## Task 2: `prefetch_script.py` —— 在沙箱里跑的预拉脚本

**Files:**
- Create: `services/orchestrator/src/orchestrator/tools/prefetch_script.py`
- Test: `services/orchestrator/tests/test_prefetch_script.py`

**Interfaces:**
- Consumes: 无(**自包含**:只用 stdlib,不 import 仓库任何模块 —— 它的源码文本要原样送进沙箱执行,沙箱里没有这个仓库)。
- Produces:
  - `MAX_FILE_BYTES: int = 32 * 1024 * 1024`
  - `MAX_TOTAL_BYTES: int = 128 * 1024 * 1024`
  - `def content_type_ok(content_type: str) -> bool`
  - `def pick_suffix(content_type: str, url: str) -> str`
  - `def target_name(var_name: str, path: list, suffix: str) -> str`
  - `def script_source() -> str` —— 本模块自己的源码文本(下发用)
  - 脚本入口:`python <script> <inputs.json 绝对路径>`,原地改写该文件

**为什么脚本是仓库里一个真实模块而不是一段字符串常量**:它的判定逻辑(content-type 白名单、扩展名、文件命名)要能被单测直接 `import` 覆盖;下发时读自己的源码即可。B-60 的 `EXEC_VIEW_SCRIPT` 只能是字面量(镜像里也要有一份),这里没有那个约束。

- [ ] **Step 1: 写失败的测试**

```python
"""B-61 Task 2 —— 预拉脚本的判定逻辑(不起沙箱,直接 import)。"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from orchestrator.tools.prefetch_script import (
    MAX_FILE_BYTES,
    MAX_TOTAL_BYTES,
    content_type_ok,
    pick_suffix,
    script_source,
    target_name,
)


@pytest.mark.parametrize(
    "content_type",
    [
        "image/jpeg",
        "image/png; charset=binary",
        "video/mp4",
        "audio/mpeg",
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/msword",
        "application/vnd.ms-excel",
    ],
)
def test_media_types_are_prefetched(content_type: str) -> None:
    assert content_type_ok(content_type) is True


@pytest.mark.parametrize(
    "content_type",
    ["text/html", "text/html; charset=utf-8", "application/json", "text/plain", ""],
)
def test_pages_and_text_are_not_prefetched(content_type: str) -> None:
    assert content_type_ok(content_type) is False


def test_suffix_prefers_the_content_type_over_the_url() -> None:
    assert pick_suffix("image/jpeg", "https://x/y") == ".jpg"
    assert pick_suffix("video/mp4", "https://x/y.bin") == ".mp4"


def test_suffix_falls_back_to_the_url_extension() -> None:
    assert pick_suffix("application/octet-stream", "https://x/y.webp") == ".webp"


def test_suffix_is_empty_when_neither_says_anything() -> None:
    assert pick_suffix("application/octet-stream", "https://x/y") == ""


def test_target_name_encodes_the_site_so_two_urls_never_collide() -> None:
    assert target_name("org_logo", [], ".jpg") == "org_logo.jpg"
    assert target_name("materials", [0, "url"], ".mp4") == "materials.0.url.mp4"


def test_target_name_rejects_traversal_in_the_variable_name() -> None:
    with pytest.raises(ValueError, match="unsafe"):
        target_name("../../etc/passwd", [], ".jpg")


def test_limits_are_the_spec_numbers() -> None:
    assert MAX_FILE_BYTES == 32 * 1024 * 1024
    assert MAX_TOTAL_BYTES == 128 * 1024 * 1024


def test_script_source_is_this_module_and_imports_only_stdlib() -> None:
    source = script_source()
    assert "def content_type_ok" in source
    tree = ast.parse(source)
    imported = {
        node.module.split(".")[0] if isinstance(node, ast.ImportFrom) and node.module else alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in (node.names or [ast.alias(name="")])
    }
    forbidden = {"orchestrator", "expert_work", "control_plane", "httpx", "pydantic"}
    assert not (imported & forbidden), f"脚本要在沙箱里跑,不能依赖仓库/三方包: {imported & forbidden}"


def test_script_source_matches_the_file_on_disk() -> None:
    path = Path(__file__).parents[1] / "src/orchestrator/tools/prefetch_script.py"
    assert script_source() == path.read_text(encoding="utf-8")
```

- [ ] **Step 2: 跑测试确认它红**

Run: `uv run --no-sync pytest services/orchestrator/tests/test_prefetch_script.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'orchestrator.tools.prefetch_script'`

- [ ] **Step 3: 写实现**

```python
"""B-61 §4.3 —— 在**沙箱里**运行的预拉脚本。

平台把本模块的源码文本送进沙箱执行:``python - <inputs.json 绝对路径>``。下载因此
发生在沙箱的出网通道上,自动继承该 agent 的 ``sandbox.network`` 策略(spec §4.3 的
治理理由:预拉是替模型做它本来要做的事,就该受同样的约束)。

**自包含**:只用 stdlib,不 import 仓库任何东西 —— 沙箱里没有这个仓库。判定逻辑因此
可以被本地单测直接 import 覆盖(``test_prefetch_script.py``)。

**永不让 run 失败**:任何一个 URL 的任何一种失败都只是把它的 ``local_path`` 写成
``null``,脚本自己始终以 0 退出。
"""

from __future__ import annotations

import json
import os
import posixpath
import sys
import urllib.error
import urllib.request
from urllib.parse import urlparse

MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_TOTAL_BYTES = 128 * 1024 * 1024
TIMEOUT_S = 30

#: 按 content-type 决定「是不是素材」。前缀族 + 精确名两张表。
_TYPE_PREFIXES = ("image/", "video/", "audio/")
_TYPE_EXACT = frozenset(
    {
        "application/pdf",
        "application/msword",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-excel",
        "application/vnd.ms-powerpoint",
    }
)

_SUFFIX_BY_TYPE = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "audio/mpeg": ".mp3",
    "audio/wav": ".wav",
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
}


def _bare_type(content_type: str) -> str:
    return content_type.split(";")[0].strip().lower()


def content_type_ok(content_type: str) -> bool:
    """这个 content-type 算「素材」吗?``text/html`` 这类是地址不是素材,不存。"""
    bare = _bare_type(content_type)
    return bare.startswith(_TYPE_PREFIXES) or bare in _TYPE_EXACT


def pick_suffix(content_type: str, url: str) -> str:
    """扩展名:content-type 优先,回落到 URL 路径的扩展名,都没有就空串。"""
    bare = _bare_type(content_type)
    if bare in _SUFFIX_BY_TYPE:
        return _SUFFIX_BY_TYPE[bare]
    ext = posixpath.splitext(urlparse(url).path)[1]
    return ext if 1 < len(ext) <= 6 and ext.isascii() else ""


def target_name(var_name: str, path: list, suffix: str) -> str:
    """文件名 = 变量名 + 位置 + 扩展名。位置进名字,两个 URL 永不撞。"""
    parts = [str(var_name), *[str(p) for p in path]]
    for part in parts:
        if not part or "/" in part or part in {".", ".."}:
            msg = f"unsafe path component: {part!r}"
            raise ValueError(msg)
    return ".".join(parts) + suffix


def script_source() -> str:
    """本模块的源码文本 —— 平台下发进沙箱执行的就是它。"""
    with open(__file__, encoding="utf-8") as handle:  # noqa: PTH123 - 沙箱里不保证有 pathlib 习惯
        return handle.read()


def _fetch(url: str, dest_dir: str, var_name: str, path: list, budget: int) -> tuple[str | None, int]:
    """拉一个 URL。返回 ``(文件名 or None, 消耗字节数)``;任何失败都返回 ``(None, 0)``。"""
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "expert-work-prefetch/1"})  # noqa: S310
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:  # noqa: S310
            content_type = response.headers.get("Content-Type", "")
            if not content_type_ok(content_type):
                return None, 0
            declared = response.headers.get("Content-Length")
            if declared is not None and declared.isdigit() and int(declared) > MAX_FILE_BYTES:
                return None, 0
            cap = min(MAX_FILE_BYTES, budget)
            body = response.read(cap + 1)
        if len(body) > cap:
            return None, 0
        name = target_name(var_name, path, pick_suffix(content_type, url))
        os.makedirs(dest_dir, exist_ok=True)
        with open(os.path.join(dest_dir, name), "wb") as handle:  # noqa: PTH118,PTH123
            handle.write(body)
        return name, len(body)
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None, 0


def main(argv: list[str]) -> int:
    inputs_path = argv[1]
    with open(inputs_path, encoding="utf-8") as handle:  # noqa: PTH123
        doc = json.load(handle)
    run_dir = os.path.dirname(inputs_path)  # noqa: PTH120
    files_dir = os.path.join(run_dir, "files")  # noqa: PTH118
    rel_prefix = posixpath.join("inputs", os.path.basename(run_dir), "files")  # noqa: PTH119
    budget = MAX_TOTAL_BYTES
    report: list[dict] = []

    for name, entry in doc.get("variables", {}).items():
        for path, url in _sites(entry.get("value"), []):
            filename, used = _fetch(url, files_dir, name, path, budget)
            budget -= used
            rel = posixpath.join(rel_prefix, filename) if filename else None
            _assign(entry, path, rel)
            report.append({"variable": name, "hit": filename is not None, "bytes": used})

    with open(inputs_path, "w", encoding="utf-8") as handle:  # noqa: PTH123
        json.dump(doc, handle, ensure_ascii=False)
    # 只打名字/命中/字节数,不打值也不打 URL(spec §八)。
    print(json.dumps({"prefetch": report}, ensure_ascii=False))
    return 0


def _sites(value, prefix: list) -> list:
    if isinstance(value, str) and (value.startswith("http://") or value.startswith("https://")):
        return [(list(prefix), value)]
    if isinstance(value, dict):
        found = []
        for key, item in value.items():
            if key != "local_path":
                found.extend(_sites(item, [*prefix, key]))
        return found
    if isinstance(value, list):
        found = []
        for index, item in enumerate(value):
            found.extend(_sites(item, [*prefix, index]))
        return found
    return []


def _assign(entry: dict, path: list, rel) -> None:
    if not path:
        entry["local_path"] = rel
        return
    cursor = entry["value"]
    for step in path[:-1]:
        cursor = cursor[step]
    cursor["local_path"] = rel


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
```

- [ ] **Step 4: 跑测试确认它绿**

Run: `uv run --no-sync pytest services/orchestrator/tests/test_prefetch_script.py -v`
Expected: PASS

- [ ] **Step 5: 补一条「定位逻辑两份必须同义」的测试**

`inputs_doc.iter_url_sites`(宿主侧)与 `prefetch_script._sites`(沙箱侧)是同一套语义的两份实现。加一条对拍:

```python
def test_site_walk_matches_the_host_side_implementation() -> None:
    from orchestrator.tools.inputs_doc import iter_url_sites
    from orchestrator.tools.prefetch_script import _sites

    doc = {
        "variables": {
            "a": {"value": "https://x/1.jpg", "trusted": True},
            "b": {"value": [{"url": "https://x/2.mp4"}, {"url": "not-a-url"}], "trusted": True},
        }
    }
    host = [(s.var_name, list(s.path), s.url) for s in iter_url_sites(doc)]
    sandbox = [
        (name, path, url)
        for name, entry in doc["variables"].items()
        for path, url in _sites(entry["value"], [])
    ]
    assert host == sandbox
```

Run: `uv run --no-sync pytest services/orchestrator/tests/test_prefetch_script.py -v`
Expected: PASS

- [ ] **Step 6: 提交**

```bash
git add services/orchestrator/src/orchestrator/tools/prefetch_script.py services/orchestrator/tests/test_prefetch_script.py
git commit -m "feat(inputs): 沙箱内预拉脚本——content-type 白名单、限额、失败即降级(B-61 T2)"
```

---

## Task 3: run-start 节点 —— 写 inputs.json、跑预拉、接进图

**Files:**
- Create: `services/orchestrator/src/orchestrator/graph_builder/inputs_node.py`
- Test: `services/orchestrator/tests/test_inputs_node.py`
- Modify: `services/orchestrator/src/orchestrator/agent_factory.py`(`:1080` 一带,与 `workspace_ingest_node` 并列)
- Modify: `services/orchestrator/src/orchestrator/graph_builder/builder.py`(`:432` 形参、`:1622` 一带装节点)
- Modify: `services/orchestrator/src/orchestrator/sse.py`(`:381` 的 `configurable` 字面量)

**Interfaces:**
- Consumes: `inputs_doc.build_inputs_doc` / `inputs_rel_path` / `inputs_abs_path`;`prefetch_script.script_source`;`orchestrator.tools.file_ops.SandboxWorkspaceWriter`(已有,走沙箱写文件);`SandboxRuntime`。
- Produces:
  - `PROMPT_INPUTS_KEY: str = "prompt_inputs"`(定义在 `sse.py` 里,与 `CANCELLATION_TOKEN_KEY` 等并列;Task 7 也要用)
  - `def make_inputs_node(*, client: SandboxRuntime, variables: tuple[PromptVariableSpec, ...]) -> MemoryNode`

- [ ] **Step 1: 写失败的测试**

```python
"""B-61 Task 3 —— run-start inputs 节点。"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import pytest

from orchestrator.graph_builder.inputs_node import make_inputs_node
from expert_work.protocol import PromptVariableSpec


class _FakeRuntime:
    """记下每次 exec 的 code,并把「沙箱里的文件系统」放在一个 dict 里。"""

    def __init__(self) -> None:
        self.files: dict[str, str] = {}
        self.execs: list[str] = []

    async def exec(self, *, code: str, **kwargs: Any) -> Any:  # noqa: ANN401
        self.execs.append(code)
        return type("R", (), {"stdout": "", "stderr": "", "exit_code": 0, "timed_out": False})()


def _config(run_id: Any, tenant_id: Any, user_id: Any, inputs: dict[str, Any]) -> dict[str, Any]:
    return {
        "configurable": {
            "tenant_id": str(tenant_id),
            "run_id": str(run_id),
            "user_id": str(user_id),
            "prompt_inputs": inputs,
        }
    }


@pytest.mark.asyncio
async def test_no_inputs_means_no_exec_at_all() -> None:
    runtime = _FakeRuntime()
    node = make_inputs_node(
        client=runtime, variables=(PromptVariableSpec(name="a"),)
    )
    await node({}, _config(uuid4(), uuid4(), uuid4(), {}))
    assert runtime.execs == []


@pytest.mark.asyncio
async def test_writes_inputs_json_then_runs_the_prefetch_script() -> None:
    runtime = _FakeRuntime()
    run_id = uuid4()
    node = make_inputs_node(
        client=runtime,
        variables=(PromptVariableSpec(name="org_logo"),),
    )
    await node({}, _config(run_id, uuid4(), uuid4(), {"org_logo": "https://x/a.jpg"}))
    assert len(runtime.execs) == 2, "一次写文件、一次跑预拉"
    write_code, prefetch_code = runtime.execs
    assert f"inputs/{run_id}/inputs.json" in write_code
    assert "def content_type_ok" in prefetch_code


@pytest.mark.asyncio
async def test_no_url_means_no_prefetch_exec() -> None:
    runtime = _FakeRuntime()
    node = make_inputs_node(client=runtime, variables=(PromptVariableSpec(name="code"),))
    await node({}, _config(uuid4(), uuid4(), uuid4(), {"code": "PRJ001"}))
    assert len(runtime.execs) == 1, "没有 URL 就不必起预拉"


@pytest.mark.asyncio
async def test_a_failing_sandbox_never_fails_the_run() -> None:
    class _Boom(_FakeRuntime):
        async def exec(self, *, code: str, **kwargs: Any) -> Any:  # noqa: ANN401
            raise RuntimeError("sandbox create failed: 504")

    node = make_inputs_node(client=_Boom(), variables=(PromptVariableSpec(name="a"),))
    assert await node({}, _config(uuid4(), uuid4(), uuid4(), {"a": "x"})) == {}


@pytest.mark.asyncio
async def test_child_runs_skip() -> None:
    runtime = _FakeRuntime()
    node = make_inputs_node(client=runtime, variables=(PromptVariableSpec(name="a"),))
    config = _config(uuid4(), uuid4(), uuid4(), {"a": "x"})
    config["configurable"]["child_run"] = True
    await node({}, config)
    assert runtime.execs == []
```

- [ ] **Step 2: 跑测试确认它红**

Run: `uv run --no-sync pytest services/orchestrator/tests/test_inputs_node.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'orchestrator.graph_builder.inputs_node'`

- [ ] **Step 3: 写实现**

节点体照 `graph_builder/workspace_ingest.py:82` 的形状(取 `configurable` 里的 id、`child_run` 跳过、`expert_work_span` 包一层、异常只 warning 不抛):

```python
"""B-61 §4.2 —— run-start 节点:把本轮声明变量落成 ``inputs.json``,再在沙箱里预拉。

位置与 ``workspace_ingest`` 同层,理由见 spec §4.2:run 启动的 config 组装有四处
(``api/runs.py`` / ``run_queue_worker.py`` / ``trigger_firing.py`` / ``orphan_sweep.py``),
在那一层做必漏;图是四条路都必经的单一入口。

**永不让 run 失败**:写文件或预拉的任何异常都只 warning,节点返回 ``{}``。模型仍可
按今天的老办法自己下载 —— 退化到 B-61 之前,不是退化到坏掉。
"""
```

节点主体:

```python
async def inputs_node(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    token = cancellation_token(config)
    token.raise_if_cancelled()
    configurable = config.get("configurable") or {}
    if configurable.get("child_run"):
        return {}
    run_id = configurable_uuid(config, "run_id")
    tenant_id = configurable_uuid(config, "tenant_id")
    if run_id is None or tenant_id is None:
        return {}
    raw_inputs = configurable.get(PROMPT_INPUTS_KEY) or {}
    doc = build_inputs_doc(run_id=run_id, variables=variables, inputs=raw_inputs)
    if doc is None or not doc["variables"]:
        return {}
    ctx = ToolContext(
        tenant_id=tenant_id,
        run_id=run_id,
        user_id=configurable_uuid(config, "user_id"),
        cancellation_token=token,
    )
    writer = SandboxWorkspaceWriter(client=client, ctx=ctx)
    with expert_work_span(ExpertWorkComponent.ORCHESTRATOR, "inputs_materialize"):
        try:
            await writer.write(
                rel=inputs_rel_path(run_id), content=json.dumps(doc, ensure_ascii=False)
            )
        except Exception:
            logger.warning("inputs.write_failed", exc_info=True)
            return {}
        if not iter_url_sites(doc):
            return {}
        try:
            await client.exec(
                code=_PREFETCH_ENTRY.format(path=inputs_abs_path(run_id)),
                agent_key=ctx_agent_key(config),
            )
        except Exception:
            logger.warning("inputs.prefetch_failed", exc_info=True)
    return {}
```

`_PREFETCH_ENTRY` 把脚本源码与入口拼成一段可 `exec_python` 的代码:

```python
_PREFETCH_ENTRY = (
    script_source()
    + "\n\nraise SystemExit(main(['prefetch', {path!r}]))\n"
)
```

**注意**:`script_source()` 在模块导入时求值一次即可(它读的是仓库里的文件,不随 run 变)。

`agent_factory.py` 装门(与 `workspace_ingest_node` 并列,`:1082` 一带):

```python
    # B-61 —— 本轮声明变量落成 inputs.json + 沙箱内预拉。门:agent 声明了变量
    # 且 sandbox runtime 已接。没有声明变量的 agent 完全不装这个节点(零副作用、
    # 零额外 acquire)。
    inputs_node = None
    if spec.spec.system_prompt.variables and env.sandbox_runtime is not None:
        inputs_node = make_inputs_node(
            client=env.sandbox_runtime,
            variables=tuple(spec.spec.system_prompt.variables),
        )
```

`builder.py` 接进图 —— 挂在 `workspace_ingest` 之前(它俩都在 START 侧的链上,`:1622` 一带):

```python
    if inputs_node is not None:
        graph.add_node("inputs", inputs_node)  # type: ignore[arg-type]
        graph.add_edge(plan_tail if plan_tail is not None else START, "inputs")
        plan_tail = "inputs"
```

`sse.py` 的 `configurable` 加键(`:381` 那个字面量里):

```python
            # B-61 —— 本轮 Dynamic-Prompt 的原始 k/v。inputs 节点据此写
            # inputs.json;tools_node 据此填绑定的工具参数。四个 run 入口都
            # 汇到这里,所以这一处就是全部。
            PROMPT_INPUTS_KEY: dict(prompt_inputs or {}),
```

- [ ] **Step 4: 跑测试确认它绿**

Run: `uv run --no-sync pytest services/orchestrator/tests/test_inputs_node.py -v`
Expected: PASS(5 项)

- [ ] **Step 5: 钉住「四个 run 入口都能到」**

```python
def test_every_run_entry_reaches_the_configurable_key() -> None:
    """四个 run 入口都调 run_agent,而 PROMPT_INPUTS_KEY 只在 run_agent 里加一次。

    这条挡的是「规矩写一处漏三处」:哪天有人把 configurable 的组装挪回入口层,
    这条测试会红。
    """
    import inspect

    from orchestrator import sse

    source = inspect.getsource(sse.run_agent)
    assert source.count("PROMPT_INPUTS_KEY") == 1
```

Run: `uv run --no-sync pytest services/orchestrator/tests/test_inputs_node.py -v`
Expected: PASS

- [ ] **Step 6: 提交**

```bash
git add services/orchestrator/src/orchestrator/graph_builder/inputs_node.py \
        services/orchestrator/tests/test_inputs_node.py \
        services/orchestrator/src/orchestrator/agent_factory.py \
        services/orchestrator/src/orchestrator/graph_builder/builder.py \
        services/orchestrator/src/orchestrator/sse.py
git commit -m "feat(inputs): run-start 节点落 inputs.json 并在沙箱内预拉(B-61 T3)"
```

---

## Task 4: `EXPERT_WORK_INPUTS` 注入 + 工具描述

**Files:**
- Modify: `services/orchestrator/src/orchestrator/tools/sandbox.py`(`:96` 的 `agent_key_envs`;`:717` 一带的 `exec_python` 描述)
- Modify: `services/orchestrator/src/orchestrator/tools/agent_sandbox.py`(`:1512` 调用点)
- Modify: `services/orchestrator/src/orchestrator/tools/file_ops.py`(`bash` 工具描述,`:592` 一带)
- Test: `services/orchestrator/tests/test_sandbox_runtime_contract.py`(已有,加用例)

**Interfaces:**
- Consumes: `inputs_doc.inputs_abs_path`。
- Produces: `def agent_key_envs(agent_key: str, *, run_id: UUID | None = None) -> dict[str, str]` —— 在原有 `PYTHONUSERBASE` 之外,`run_id` 非 `None` 时多一个 `EXPERT_WORK_INPUTS`。

**为什么走这里**:两个后端已经共用这一条 per-exec env 通道(`HTTPSupervisorRuntime` → `ExecRequest.envs`;`AgentSandboxClient` → `commands.run(envs=...)`),而且被 `test_sandbox_runtime_contract.py` 钉成逐字节相同。B-60 的 `EXEC_VIEW_SCRIPT` 字面量一个字都不用动。

- [ ] **Step 1: 写失败的测试**

```python
def test_exec_envs_carry_the_inputs_path_when_a_run_is_bound() -> None:
    from uuid import UUID

    from orchestrator.tools.sandbox import agent_key_envs

    run_id = UUID("382f6f5a-55c4-49be-ac05-32fa143f010d")
    envs = agent_key_envs("ai-health-plan-30817804", run_id=run_id)
    assert envs["EXPERT_WORK_INPUTS"] == f"/workspace/inputs/{run_id}/inputs.json"
    assert envs["PYTHONUSERBASE"].endswith("ai-health-plan-30817804"), "原有隔离不能丢"


def test_exec_envs_without_a_run_are_unchanged() -> None:
    from orchestrator.tools.sandbox import agent_key_envs

    assert set(agent_key_envs("k")) == {"PYTHONUSERBASE"}


@pytest.mark.asyncio
async def test_both_backends_send_byte_identical_inputs_env(...) -> None:
    """沿用本文件既有的两后端对拍夹具,断言两边送出的 envs 完全相同。"""
```

- [ ] **Step 2: 跑测试确认它红**

Run: `uv run --no-sync pytest services/orchestrator/tests/test_sandbox_runtime_contract.py -k inputs -v`
Expected: FAIL —— `TypeError: agent_key_envs() got an unexpected keyword argument 'run_id'`

- [ ] **Step 3: 写实现**

```python
def agent_key_envs(agent_key: str, *, run_id: UUID | None = None) -> dict[str, str]:
    """Per-agent env overrides for an ``exec`` call (spec 决策 10 + B-61 §4.4).

    ``PYTHONUSERBASE`` 隔离照旧。``run_id`` 非 ``None`` 时再加 ``EXPERT_WORK_INPUTS``
    —— 本轮 ``inputs.json`` 在沙箱里的绝对路径。选环境变量而不是把路径写进提示词:
    ``os.environ["EXPERT_WORK_INPUTS"]`` 是固定写法,而路径里的 run_id 仍然是一个要
    模型手抄的串,那与 B-61 的目的自相矛盾。
    """
    envs: dict[str, str] = {}
    if agent_key:
        envs["PYTHONUSERBASE"] = f"{SANDBOX_AGENTS_ROOT}/{agent_key}"
    if run_id is not None:
        envs["EXPERT_WORK_INPUTS"] = inputs_abs_path(run_id)
    return envs
```

两个调用点把 `run_id=ctx.run_id` 传进去(`sandbox.py:291` 一带、`agent_sandbox.py:1512`)。

`exec_python` 描述(`sandbox.py:717` 一带)追加一句,`bash` 同款:

```
本轮的输入变量在 $EXPERT_WORK_INPUTS 指向的 JSON 文件里（含 URL、编码、本地文件路径）。
需要用到某个输入值时，用代码读这个文件，不要从上文手抄——长串抄错一位就是 404。
文件里 local_path 非空表示平台已把该文件下载到本地，直接用它，不必再联网下载。
```

- [ ] **Step 4: 跑测试确认它绿**

Run: `uv run --no-sync pytest services/orchestrator/tests/test_sandbox_runtime_contract.py -v`
Expected: PASS(含既有全部用例)

- [ ] **Step 5: 变异自证**

把 `if run_id is not None:` 改成 `if False:`,重跑 —— 两条新用例必须红;改回变绿。

- [ ] **Step 6: 提交**

```bash
git add services/orchestrator/src/orchestrator/tools/sandbox.py \
        services/orchestrator/src/orchestrator/tools/agent_sandbox.py \
        services/orchestrator/src/orchestrator/tools/file_ops.py \
        services/orchestrator/tests/test_sandbox_runtime_contract.py
git commit -m "feat(inputs): exec 注入 EXPERT_WORK_INPUTS + 工具描述改为「读文件不要手抄」(B-61 T4)"
```

---

## Task 5: protocol —— `arg_bindings` 字段与跨字段校验

**Files:**
- Modify: `packages/expert-work-protocol/src/expert_work/protocol/agent_spec.py`(`MCPToolSpec` 在 `:1089`;`AgentSpecBody` 在 `:1241`)
- Modify: `packages/expert-work-protocol/src/expert_work/protocol/__init__.py`(导出 `ArgBindingSpec`)
- Test: `packages/expert-work-protocol/tests/test_arg_bindings_spec.py`

**Interfaces:**
- Produces:
  - `class ArgBindingSpec(BaseModel)`:`server: str`(min_length=1)、`tool: str`(min_length=1)、`args: dict[str, str]`(参数名 → 声明变量名,min_length=1)
  - `MCPToolSpec.arg_bindings: list[ArgBindingSpec] = Field(default_factory=list)`
  - `AgentSpecBody` 的 `@model_validator(mode="after") def _check_arg_bindings(self) -> AgentSpecBody`

- [ ] **Step 1: 写失败的测试**

```python
"""B-61 Task 5 —— arg_bindings 的形状与跨字段校验。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from expert_work.protocol import AgentSpec


def _manifest(*, variables: list[dict], bindings: list[dict]) -> dict:
    return {
        "apiVersion": "expert-work/v1",
        "kind": "Agent",
        "metadata": {"name": "t"},
        "spec": {
            "model": {"provider": "glm", "name": "glm-5.3"},
            "system_prompt": {"template": "x", "jinja": True, "variables": variables},
            "tools": [
                {"type": "mcp", "servers": ["deepcare"], "arg_bindings": bindings},
            ],
        },
    }


def test_binding_to_a_declared_variable_is_accepted() -> None:
    spec = AgentSpec.model_validate(
        _manifest(
            variables=[{"name": "project_code"}],
            bindings=[{"server": "deepcare", "tool": "customer_search", "args": {"project_code": "project_code"}}],
        )
    )
    entry = spec.spec.tools[0]
    assert entry.arg_bindings[0].args == {"project_code": "project_code"}


def test_binding_to_an_undeclared_variable_is_rejected() -> None:
    with pytest.raises(ValidationError, match="not a declared prompt variable"):
        AgentSpec.model_validate(
            _manifest(
                variables=[{"name": "project_code"}],
                bindings=[{"server": "deepcare", "tool": "customer_search", "args": {"p": "typo_code"}}],
            )
        )


def test_deleting_a_variable_that_a_binding_uses_is_rejected() -> None:
    """删变量与绑定悬空是同一条校验的两面 —— 删掉 project_code 后就是上一条。"""
    with pytest.raises(ValidationError, match="not a declared prompt variable"):
        AgentSpec.model_validate(
            _manifest(
                variables=[],
                bindings=[{"server": "deepcare", "tool": "customer_search", "args": {"p": "project_code"}}],
            )
        )


def test_two_bindings_for_the_same_server_and_tool_are_rejected() -> None:
    with pytest.raises(ValidationError, match="duplicate arg_bindings"):
        AgentSpec.model_validate(
            _manifest(
                variables=[{"name": "a"}],
                bindings=[
                    {"server": "deepcare", "tool": "t", "args": {"x": "a"}},
                    {"server": "deepcare", "tool": "t", "args": {"y": "a"}},
                ],
            )
        )


def test_empty_args_is_rejected() -> None:
    with pytest.raises(ValidationError):
        AgentSpec.model_validate(
            _manifest(
                variables=[{"name": "a"}],
                bindings=[{"server": "deepcare", "tool": "t", "args": {}}],
            )
        )


def test_default_is_empty_so_existing_manifests_are_untouched() -> None:
    spec = AgentSpec.model_validate(_manifest(variables=[], bindings=[]))
    assert spec.spec.tools[0].arg_bindings == []
```

- [ ] **Step 2: 跑测试确认它红**

Run: `uv run --no-sync pytest packages/expert-work-protocol/tests/test_arg_bindings_spec.py -v`
Expected: FAIL —— `ValidationError: Extra inputs are not permitted [type=extra_forbidden]`(`MCPToolSpec` 是 `extra="forbid"`)

- [ ] **Step 3: 写实现**

```python
class ArgBindingSpec(BaseModel):
    """B-61 —— 把一个 MCP 工具的若干参数绑定到本 agent 的声明变量。

    绑定后:该参数从**给模型的 JSON schema 里删掉**,由平台在 ``tools_node`` 按
    本轮 ``inputs`` 填入。模型既看不到值,也没有机会抄错(spec §五)。

    ``server`` 必填 —— wire 名是 ``mcp__<server>__<tool>``,跨服务器同名工具会撞;
    而同名参数在不同服务器上很可能不是一回事(``project_code`` 是最典型的),所以
    **没有**按参数名的全局规则,一条一条显式列(spec §十一 之二)。
    """

    model_config = ConfigDict(extra="forbid")

    server: str = Field(min_length=1)
    tool: str = Field(min_length=1)
    #: 工具参数名 → 声明变量名。至少一项。
    args: dict[str, str] = Field(min_length=1)
```

`MCPToolSpec` 追加字段:

```python
    #: B-61 —— 逐工具的参数绑定,默认空(存量 manifest 零影响)。
    arg_bindings: list[ArgBindingSpec] = Field(default_factory=list)
```

`AgentSpecBody` 的跨字段校验(与 `AgentSpec._check_subagents` 同形):

```python
    @model_validator(mode="after")
    def _check_arg_bindings(self) -> AgentSpecBody:
        """B-61 —— 绑定只能指向本 agent 声明过的变量,且一个 (server, tool) 只配一次。

        下拉框挡不住三条来路:配置页有 YAML 直编、后端有 ``PUT …/draft``、模板复制
        会把绑定带到变量集不同的 agent 上;再加上时间 —— 先绑好、之后把变量删了。
        所以这道闸在 manifest 层,不在前端。
        """
        declared = {v.name for v in self.system_prompt.variables}
        seen: set[tuple[str, str]] = set()
        for entry in self.tools:
            if not isinstance(entry, MCPToolSpec):
                continue
            for binding in entry.arg_bindings:
                key = (binding.server, binding.tool)
                if key in seen:
                    msg = f"duplicate arg_bindings for server={binding.server!r} tool={binding.tool!r}"
                    raise ValueError(msg)
                seen.add(key)
                for param, var_name in binding.args.items():
                    if var_name not in declared:
                        msg = (
                            f"arg_bindings[{binding.server}/{binding.tool}].{param} → "
                            f"{var_name!r} is not a declared prompt variable "
                            f"(system_prompt.variables)"
                        )
                        raise ValueError(msg)
        return self
```

- [ ] **Step 4: 跑测试确认它绿**

Run: `uv run --no-sync pytest packages/expert-work-protocol/tests/test_arg_bindings_spec.py -v`
Expected: PASS(6 项)

- [ ] **Step 5: 变异自证**

把 `if var_name not in declared:` 改成 `if False:` —— 第 2、3 条必须红;改回变绿。

- [ ] **Step 6: 提交**

```bash
git add packages/expert-work-protocol/src/expert_work/protocol/agent_spec.py \
        packages/expert-work-protocol/src/expert_work/protocol/__init__.py \
        packages/expert-work-protocol/tests/test_arg_bindings_spec.py
git commit -m "feat(protocol): MCPToolSpec.arg_bindings + 绑定只能指向声明变量的跨字段校验(B-61 T5)"
```

---

## Task 6: `arg_bindings.py` —— 剥 schema 与填值的纯函数

**Files:**
- Create: `services/orchestrator/src/orchestrator/tools/arg_bindings.py`
- Test: `services/orchestrator/tests/test_arg_bindings.py`

**Interfaces:**
- Consumes: `expert_work.protocol.ArgBindingSpec`。
- Produces:
  - `def bindings_by_tool(entries: Sequence[ArgBindingSpec], *, server: str) -> dict[str, dict[str, str]]` —— 该服务器下 `{裸工具名: {参数名: 变量名}}`
  - `def strip_bound_params(input_schema: Mapping[str, Any], bound: Collection[str]) -> dict[str, Any]` —— 返回新 schema,删 `properties` 里的键并从 `required` 移除
  - `def apply_arg_bindings(tool_calls: Sequence[Mapping[str, Any]], *, bindings: Mapping[str, Mapping[str, str]], inputs: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[str]]` —— 返回(新 tool_calls, 本次实际填入的参数名,用于审计)

- [ ] **Step 1: 写失败的测试**

```python
"""B-61 Task 6 —— 绑定的纯函数。"""

from __future__ import annotations

from expert_work.protocol import ArgBindingSpec

from orchestrator.tools.arg_bindings import (
    apply_arg_bindings,
    bindings_by_tool,
    strip_bound_params,
)

SCHEMA = {
    "type": "object",
    "properties": {
        "project_code": {"type": "string"},
        "keyword": {"type": "string"},
    },
    "required": ["project_code", "keyword"],
}


def test_bindings_are_selected_by_server() -> None:
    entries = [
        ArgBindingSpec(server="deepcare", tool="t1", args={"project_code": "pc"}),
        ArgBindingSpec(server="other", tool="t1", args={"project_code": "pc"}),
    ]
    assert bindings_by_tool(entries, server="deepcare") == {"t1": {"project_code": "pc"}}


def test_stripping_removes_the_property_and_the_required_entry() -> None:
    stripped = strip_bound_params(SCHEMA, {"project_code"})
    assert set(stripped["properties"]) == {"keyword"}
    assert stripped["required"] == ["keyword"]
    # 原 schema 不变
    assert set(SCHEMA["properties"]) == {"project_code", "keyword"}


def test_stripping_an_absent_param_is_a_no_op() -> None:
    assert strip_bound_params(SCHEMA, {"nope"}) == SCHEMA


def test_apply_fills_the_bound_arg_from_this_runs_inputs() -> None:
    calls = [{"name": "mcp__deepcare__t1", "args": {"keyword": "王"}, "id": "c1"}]
    filled, names = apply_arg_bindings(
        calls,
        bindings={"mcp__deepcare__t1": {"project_code": "pc"}},
        inputs={"pc": "PRJ001"},
    )
    assert filled[0]["args"] == {"keyword": "王", "project_code": "PRJ001"}
    assert names == ["project_code"]
    assert calls[0]["args"] == {"keyword": "王"}, "原 tool_calls 不可变"


def test_a_missing_optional_variable_leaves_the_arg_unfilled() -> None:
    calls = [{"name": "mcp__deepcare__t1", "args": {}, "id": "c1"}]
    filled, names = apply_arg_bindings(
        calls, bindings={"mcp__deepcare__t1": {"project_code": "pc"}}, inputs={}
    )
    assert filled[0]["args"] == {}
    assert names == []


def test_a_model_supplied_value_never_wins_over_the_binding() -> None:
    """schema 里已经没这个参数了,模型还是塞了一个 —— 平台的值覆盖它。"""
    calls = [{"name": "mcp__deepcare__t1", "args": {"project_code": "伪造"}, "id": "c1"}]
    filled, _ = apply_arg_bindings(
        calls, bindings={"mcp__deepcare__t1": {"project_code": "pc"}}, inputs={"pc": "PRJ001"}
    )
    assert filled[0]["args"]["project_code"] == "PRJ001"


def test_unbound_tools_pass_through_untouched() -> None:
    calls = [{"name": "exec_python", "args": {"code": "print(1)"}, "id": "c1"}]
    filled, names = apply_arg_bindings(calls, bindings={}, inputs={"pc": "x"})
    assert filled == calls
    assert names == []
```

- [ ] **Step 2: 跑测试确认它红**

Run: `uv run --no-sync pytest services/orchestrator/tests/test_arg_bindings.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'orchestrator.tools.arg_bindings'`

- [ ] **Step 3: 写实现**

```python
"""B-61 §五 —— 声明变量绑定到 MCP 工具参数,全是纯函数。

两件事分在两层(spec §5.2 / §5.3):

* **剥 schema** 在建工具目录时做 —— 绑定属于 spec 而非 run,跟 BuiltAgent 缓存天然一致。
* **填值** 在 ``tools_node`` 最前面做 —— 审批门与 action screening 都在 dispatch 之前
  读 args,填在最前面,人审批时看到的是真值。

平台的值**永远覆盖**模型给的同名参数:schema 里已经没有那个字段了,模型还塞进来,
只可能是幻觉或注入。
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from typing import Any

from expert_work.protocol import ArgBindingSpec

from orchestrator.tools.mcp import mcp_tool_name


def bindings_by_tool(
    entries: Sequence[ArgBindingSpec], *, server: str
) -> dict[str, dict[str, str]]:
    """该服务器下的 ``{裸工具名: {参数名: 变量名}}``。"""
    return {e.tool: dict(e.args) for e in entries if e.server == server}


def strip_bound_params(
    input_schema: Mapping[str, Any], bound: Collection[str]
) -> dict[str, Any]:
    """返回一份新 schema:删掉被绑定的参数,并从 ``required`` 里移除。"""
    properties = input_schema.get("properties")
    if not isinstance(properties, Mapping):
        return dict(input_schema)
    removed = {name for name in bound if name in properties}
    if not removed:
        return dict(input_schema)
    stripped = {k: v for k, v in properties.items() if k not in removed}
    out = {**input_schema, "properties": stripped}
    required = input_schema.get("required")
    if isinstance(required, Sequence) and not isinstance(required, str):
        out["required"] = [name for name in required if name not in removed]
    return out


def apply_arg_bindings(
    tool_calls: Sequence[Mapping[str, Any]],
    *,
    bindings: Mapping[str, Mapping[str, str]],
    inputs: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    """把绑定的参数填进每个 tool_call,返回(新 calls, 实际填入的参数名)。

    变量本轮没传(``required: false`` 的可选变量)→ 该参数**不填**。schema 里也没有
    这个字段,MCP 服务端会按自己的 required 报错给模型,模型能看懂并改道 —— 不静默。
    """
    filled: list[dict[str, Any]] = []
    names: list[str] = []
    for call in tool_calls:
        bound = bindings.get(str(call.get("name", "")))
        if not bound:
            filled.append(dict(call))
            continue
        args = dict(call.get("args") or {})
        for param, var_name in bound.items():
            if var_name in inputs:
                args[param] = inputs[var_name]
                names.append(param)
        filled.append({**call, "args": args})
    return filled, names
```

- [ ] **Step 4: 跑测试确认它绿**

Run: `uv run --no-sync pytest services/orchestrator/tests/test_arg_bindings.py -v`
Expected: PASS(7 项)

- [ ] **Step 5: 变异自证**

把 `args[param] = inputs[var_name]` 改成 `args.setdefault(param, inputs[var_name])` —— `test_a_model_supplied_value_never_wins_over_the_binding` 必须红。改回变绿。

- [ ] **Step 6: 提交**

```bash
git add services/orchestrator/src/orchestrator/tools/arg_bindings.py services/orchestrator/tests/test_arg_bindings.py
git commit -m "feat(tools): 绑定的纯函数——剥 schema 与填值(B-61 T6)"
```

---

## Task 7: 接线 —— 建目录时剥 schema,`tools_node` 填值

**Files:**
- Modify: `services/orchestrator/src/orchestrator/tools/mcp.py`(`register_mcp_tools` 在 `:944`)
- Modify: `services/orchestrator/src/orchestrator/tools/assembly.py`(`_register_mcp` 在 `:688`,四个 `register_mcp_tools(...)` 调用点全要传)
- Modify: `services/orchestrator/src/orchestrator/graph_builder/builder.py`(`tools_node` 在 `:1270`,`_extract_tool_calls` 在 `:1275`)
- Test: `services/orchestrator/tests/test_mcp_arg_bindings_wiring.py`

**Interfaces:**
- Consumes: Task 6 的三个函数;Task 3 的 `PROMPT_INPUTS_KEY`。
- Produces: `register_mcp_tools(..., arg_bindings: Mapping[str, Mapping[str, str]] | None = None)`;`tools_node` 内部把绑定表从 `tool_registry` 取出(见实现)。

- [ ] **Step 1: 写失败的测试**

```python
"""B-61 Task 7 —— 绑定的接线:模型看不到被绑参数、填值发生在审批之前。"""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_the_model_never_sees_a_bound_parameter(mcp_registry_factory) -> None:
    """建完目录后,tool catalog 里那个参数连同 required 一起消失。"""
    registry = await mcp_registry_factory(
        tools=[{"name": "t1", "input_schema": {
            "type": "object",
            "properties": {"project_code": {"type": "string"}, "keyword": {"type": "string"}},
            "required": ["project_code", "keyword"],
        }}],
        arg_bindings={"t1": {"project_code": "pc"}},
    )
    schema = registry.get_required("mcp__deepcare__t1").spec.input_schema
    assert set(schema["properties"]) == {"keyword"}
    assert schema["required"] == ["keyword"]


@pytest.mark.asyncio
async def test_bound_args_are_filled_before_the_approval_gate(graph_harness) -> None:
    """审批请求里带的是**真值** —— 填值在 tools_node 最前面,不在 dispatch 前那层。"""
    harness = graph_harness(
        approval_required_tools={"mcp__deepcare__t1"},
        bindings={"mcp__deepcare__t1": {"project_code": "pc"}},
        prompt_inputs={"pc": "PRJ001"},
    )
    update = await harness.run_turn_with_tool_call("mcp__deepcare__t1", {"keyword": "王"})
    approval = update["pending_approval"]
    assert approval.tool_call["args"]["project_code"] == "PRJ001"


@pytest.mark.asyncio
async def test_bound_args_are_filled_before_action_screening(graph_harness) -> None:
    harness = graph_harness(
        action_screen="block",
        bindings={"mcp__deepcare__t1": {"project_code": "pc"}},
        prompt_inputs={"pc": "PRJ001"},
    )
    await harness.run_turn_with_tool_call("mcp__deepcare__t1", {"keyword": "王"})
    judged = harness.judge_calls[-1]
    assert judged["tool_args"]["project_code"] == "PRJ001"


@pytest.mark.asyncio
async def test_audit_records_which_params_the_platform_filled(graph_harness) -> None:
    harness = graph_harness(
        bindings={"mcp__deepcare__t1": {"project_code": "pc"}},
        prompt_inputs={"pc": "PRJ001"},
    )
    await harness.run_turn_with_tool_call("mcp__deepcare__t1", {"keyword": "王"})
    row = harness.audit_rows[-1]
    assert row.details["bound_args"] == ["project_code"]
    assert "PRJ001" not in str(row.details), "只记参数名,不记值"


@pytest.mark.asyncio
async def test_a_binding_whose_param_is_absent_from_the_schema_warns_but_runs(
    mcp_registry_factory, caplog
) -> None:
    registry = await mcp_registry_factory(
        tools=[{"name": "t1", "input_schema": {"type": "object", "properties": {"keyword": {}}}}],
        arg_bindings={"t1": {"gone": "pc"}},
    )
    assert registry.get_required("mcp__deepcare__t1") is not None
    assert "mcp.binding_param_absent" in caplog.text
```

夹具 `mcp_registry_factory` / `graph_harness` 放在 `services/orchestrator/tests/conftest.py`,照该文件既有的 MCP 假客户端与图夹具写法扩展(已有 `FakeMCPClient` 与图构建夹具,直接加参数,不要新造一套)。

- [ ] **Step 2: 跑测试确认它红**

Run: `uv run --no-sync pytest services/orchestrator/tests/test_mcp_arg_bindings_wiring.py -v`
Expected: FAIL —— `TypeError: register_mcp_tools() got an unexpected keyword argument 'arg_bindings'`

- [ ] **Step 3: 写实现**

`register_mcp_tools` 里(`:970` 的循环内)剥 schema:

```python
    for tool_def in tools:
        if allow_tools is not None and tool_def.name not in allow_tools:
            continue
        bound = (arg_bindings or {}).get(tool_def.name) or {}
        if bound:
            absent = [p for p in bound if p not in (tool_def.input_schema.get("properties") or {})]
            if absent:
                # 上游改了接口,绑定指向一个不存在的参数。不阻断 run:这个 agent
                # 的其它工具照常可用,该条按未命中处理(spec §5.4)。
                logger.warning(
                    "mcp.binding_param_absent server=%s tool=%s params=%s",
                    server_name,
                    tool_def.name,
                    absent,
                )
            tool_def = replace(
                tool_def, input_schema=strip_bound_params(tool_def.input_schema, bound)
            )
        expert_work_tool = MCPTool(...)
```

`_register_mcp` 在四个 `register_mcp_tools(...)` 调用点各传一次:

```python
            await register_mcp_tools(
                server_name=server_name,
                client=client,
                registry=registry,
                allow_tools=allow,
                deferred=True,
                arg_bindings=bindings_by_tool(entry.arg_bindings, server=server_name),
            )
```

`tools_node` 填值(`builder.py:1275` 之后,**在审批与 action screening 之前**):

```python
        tool_calls = _extract_tool_calls(last)
        if not tool_calls:
            return {}

        # B-61 §5.3 —— 平台绑定的参数在这里填,早于审批门与 action screening:
        # 两者都在 dispatch 之前读 args,填在这里,人审批时看到的是真值、judge
        # 也是对真参数判对齐。填在 before_tool_dispatch 那层就都看不到了。
        configurable_now = config.get("configurable") or {}
        tool_calls, bound_arg_names = apply_arg_bindings(
            tool_calls,
            bindings=tool_registry.arg_bindings(),
            inputs=configurable_now.get(PROMPT_INPUTS_KEY) or {},
        )
```

`ToolRegistry` 增加 `arg_bindings() -> Mapping[str, Mapping[str, str]]`(命名空间名 → 参数 → 变量),由 `register_mcp_tools` 注册时用 `mcp_tool_name(server_name, tool_def.name)` 作键填进去。审计 `TOOL_CALL` 行的 `details` 加 `"bound_args": bound_arg_names`。

- [ ] **Step 4: 跑测试确认它绿**

Run: `uv run --no-sync pytest services/orchestrator/tests/test_mcp_arg_bindings_wiring.py -v`
Expected: PASS(5 项)

- [ ] **Step 5: 变异自证**

把 `tools_node` 里的填值调用整段挪到 `_dispatch_tool` 的 `before_tool_dispatch` 之后 —— 「审批看到真值」和「judge 看到真值」两条必须红。挪回变绿。

- [ ] **Step 6: 提交**

```bash
git add services/orchestrator/src/orchestrator/tools/mcp.py \
        services/orchestrator/src/orchestrator/tools/assembly.py \
        services/orchestrator/src/orchestrator/tools/registry.py \
        services/orchestrator/src/orchestrator/graph_builder/builder.py \
        services/orchestrator/tests/test_mcp_arg_bindings_wiring.py \
        services/orchestrator/tests/conftest.py
git commit -m "feat(tools): 绑定接线——建目录剥 schema,tools_node 在审批前填值(B-61 T7)"
```

---

## Task 8: 配置页 —— 逐工具逐参数「自动 / 绑定」

**Files:**
- Modify: `apps/admin-ui/src/components/manifest-editor/widgets/McpToolPicker.tsx`
- Modify: `apps/admin-ui/src/components/manifest-editor/FormView.tsx`(`:509` 一带,把 `promptVariables` 与 `argBindings` 传进去)
- Test: `apps/admin-ui/src/components/manifest-editor/widgets/__tests__/McpToolPicker.bindings.test.tsx`

**Interfaces:**
- Consumes:后端探针已经返回每个工具的 `input_schema`(`api/mcp_servers.py:1068`、`api/mcp_catalog.py:316`)—— **不需要任何后端改动**。
- Produces:`onChange(servers, allowTools, argBindings)`,`argBindings` 形状与 manifest 的 `arg_bindings` 一致。

- [ ] **Step 1: 写失败的测试**

```tsx
/** B-61 Task 8 —— 逐参数绑定的配置 UI。 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

describe("McpToolPicker 参数绑定", () => {
  it("展开的工具按 input_schema 逐参数给出「自动 / 绑定变量」", async () => {
    renderPicker({ tools: [{ name: "t1", input_schema: { properties: { project_code: {}, keyword: {} } } }] });
    await userEvent.click(await screen.findByRole("button", { name: /t1/ }));
    expect(screen.getByLabelText("project_code")).toBeInTheDocument();
    expect(screen.getByLabelText("keyword")).toBeInTheDocument();
  });

  it("下拉只列本 agent 声明过的变量", async () => {
    renderPicker({ promptVariables: ["project_code", "employee_code"] });
    await userEvent.click(await screen.findByRole("button", { name: /t1/ }));
    await userEvent.click(screen.getByLabelText("project_code"));
    expect(screen.getAllByRole("option").map((o) => o.textContent)).toEqual([
      "自动（模型填）",
      "project_code",
      "employee_code",
    ]);
  });

  it("选中变量后 onChange 带出 manifest 形状的 arg_bindings", async () => {
    const onChange = vi.fn();
    renderPicker({ onChange, promptVariables: ["project_code"] });
    await userEvent.click(await screen.findByRole("button", { name: /t1/ }));
    await userEvent.click(screen.getByLabelText("project_code"));
    await userEvent.click(screen.getByRole("option", { name: "project_code" }));
    await waitFor(() =>
      expect(onChange).toHaveBeenLastCalledWith(["deepcare"], expect.anything(), [
        { server: "deepcare", tool: "t1", args: { project_code: "project_code" } },
      ]),
    );
  });

  it("取消绑定会把该条从 arg_bindings 里移掉,空了整条删除", async () => {
    const onChange = vi.fn();
    renderPicker({
      onChange,
      promptVariables: ["project_code"],
      argBindings: [{ server: "deepcare", tool: "t1", args: { project_code: "project_code" } }],
    });
    await userEvent.click(await screen.findByRole("button", { name: /t1/ }));
    await userEvent.click(screen.getByLabelText("project_code"));
    await userEvent.click(screen.getByRole("option", { name: "自动（模型填）" }));
    await waitFor(() => expect(onChange).toHaveBeenLastCalledWith(["deepcare"], expect.anything(), []));
  });

  it("声明变量为空时给出提示而不是一个空下拉", async () => {
    renderPicker({ promptVariables: [] });
    await userEvent.click(await screen.findByRole("button", { name: /t1/ }));
    expect(screen.getByText(/先在「提示词变量」里声明变量/)).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: 跑测试确认它红**

Run: `pnpm -C apps/admin-ui test -- McpToolPicker.bindings`
Expected: FAIL —— 找不到参数行。

- [ ] **Step 3: 写实现**

在 `McpToolPicker` 每个已勾选工具的展开区里,按 `input_schema.properties` 逐参数渲染一行:左边参数名(必填的加 `*`),右边一个 antd `Select`,选项 = `自动（模型填）` + 该 agent 的声明变量。选中即写进 `argBindings`;选回「自动」就删掉该参数,该工具的 `args` 空了就整条删除。

`FormView.tsx:509` 一带把两个新 prop 传下去:`promptVariables={form.system_prompt.variables.map(v => v.name)}` 与 `argBindings={...}`,`onChange` 的第三个参数写回 manifest 的 `tools[].arg_bindings`。

- [ ] **Step 4: 跑测试确认它绿**

Run: `pnpm -C apps/admin-ui test -- McpToolPicker.bindings`
Expected: PASS(5 项)

- [ ] **Step 5: 类型检查(裸 `tsc` 恒绿,必须走这条)**

Run: `pnpm -C apps/admin-ui typecheck`
Expected: 无错误

- [ ] **Step 6: 提交**

```bash
git add apps/admin-ui/src/components/manifest-editor/widgets/McpToolPicker.tsx \
        apps/admin-ui/src/components/manifest-editor/FormView.tsx \
        apps/admin-ui/src/components/manifest-editor/widgets/__tests__/McpToolPicker.bindings.test.tsx
git commit -m "feat(admin-ui): MCP 工具逐参数「自动/绑定变量」(B-61 T8)"
```

---

## Task 9: 文档

**Files:**
- Modify: `docs/superpowers/ROADMAP.md`(B-61 行)
- Modify: 对外文档的 run inputs 章节(按 `docs/superpowers/specs/2026-08-17-external-docs-style-guide.md` 自检)
- Create: `docs/design/agent-config-1235-addendum-b61.md`(Agent 配置书 #1235 的补充)

- [ ] **Step 1: 对外文档**

在 run inputs 的章节加一节「输入是怎么到达 agent 的」,给出 `inputs.json` 的字段表(`value` / `trusted` / `local_path` 三列穷举取值)与一段读取示例:

```python
import json, os
inputs = json.load(open(os.environ["EXPERT_WORK_INPUTS"]))["variables"]
logo = inputs["org_logo"]
path = logo.get("local_path") or download(logo["value"])   # 平台没拉到才自己下
```

说明三条:目录按 run 隔离、`local_path` 相对 `/workspace`、预拉失败不影响 run。

- [ ] **Step 2: #1235 addendum**

写明 `ai-health-plan` 模板要改的 5 处(spec §七 已列),每处给出「改前 / 改后」两段原文。强调一条硬规则:**不要用 `exec_python` 打印 inputs.json 再把值手打进 MCP 参数** —— 从工具输出抄比从提示词抄更糟。

- [ ] **Step 3: ROADMAP 销案**

B-61 行写明:两半各自的 PR 编号、上线版本、以及「对接方改提示词是第二步,他们自己挑时间」。

- [ ] **Step 4: 提交**

```bash
git add docs/
git commit -m "docs: B-61 对外文档 + #1235 addendum + ROADMAP 销案"
```

---

## Task 10: 真栈验收(最后跑 —— 在 T11/T12 之后)

**不是代码任务。** 逐条打勾,任一条不过就停。**不碰对接方的两个 agent**。

- [ ] **Step 1: 发测试环境** —— `tools/deploy/release.sh test`,smoke 全绿、金丝雀 PASS,按惯例开 `chore(deploy)` 记录 PR。

- [ ] **Step 2: 数据面** —— 用金丝雀 agent(或探针用户)跑一个带两类变量的 run:一个普通编码值、一个指向公网图片的 URL。凭据走 stdin 首行,不进 argv、不落文件。
  Expected:
  - NAS 上 `agents/<key>/inputs/<run_id>/inputs.json` 存在,`variables` 两项齐全;
  - 图片那项 `local_path` 非空,且 `agents/<key>/inputs/cache/<digest>.jpg` 字节数 > 0(T11 起是内容寻址的共享缓存,不再按 run 复制);
  - `exec_python` 里 `os.environ["EXPERT_WORK_INPUTS"]` 指向该文件且可读。

- [ ] **Step 3: 预拉降级** —— 同样的 agent,变量给一个 404 的 URL 和一个 `text/html` 的网页地址。
  Expected:两项 `local_path` 均为 `null`,**run 仍然 success**。

- [ ] **Step 4: 绑定面** —— 给探针 agent 配一个 MCP 工具的参数绑定,跑一次。
  Expected:模型的 tool catalog 里没有该参数(查 run_event 的工具 schema 快照);`TOOL_CALL` 审计行的 `bound_args` 列出该参数名且不含值;MCP 服务端收到的就是 inputs 里的值。

- [ ] **Step 4.5: 缓存与清扫(T11/T12 的验收,与本批同一次发布一起验)**
  - **缓存真命中**:同一 agent 连跑两轮同样的变量 —— 第二轮预拉报告里该 URL `hit=true` 且 `bytes=0`;
    NAS 上 `agents/<key>/inputs/cache/` 只有一份文件。
  - **清扫删对了**:control-plane pod 里手动跑一次 janitor 的 `run_once()`,确认超期的 `cache/` 文件与 per-run 目录消失、
    体积记账跟着刷新。
  - **清扫没误删**(关键):起一个正在跑的 run,同时触发 janitor,确认它的 `inputs/<run_id>/inputs.json` **没被动**。
    前两条只证明它删了该删的,这条才证明它没删不该删的 —— 而删错会当场打断执行。

- [ ] **Step 5: 回滚演练的前置确认** —— 确认发布清单里写了 `MCPToolSpec` 是 `extra="forbid"`、回滚窗口内先别配绑定(spec §七)。

- [ ] **Step 6: 通过后** —— ROADMAP B-61 销案;通知对接方可以按 addendum 改提示词。

---

## Task 11: 预拉改为内容寻址缓存

**Files:**
- Modify: `services/orchestrator/src/orchestrator/tools/prefetch_script.py`
- Modify: `services/orchestrator/src/orchestrator/graph_builder/inputs_node.py`(只改 `_PREFETCH_TIMEOUT_S` 的注释,见 Step 4)
- Test: `services/orchestrator/tests/test_prefetch_script.py`

**为什么**:今天按 run 复制,同一个 logo/视频**每轮重下一份**。每用户工作区配额默认 10 GiB
(`DEFAULT_WORKSPACE_BYTES_PER_USER`)且挂在 `AgentSandboxClient.acquire` 的闸上(`agent_sandbox.py:575-578`)——
超了直接 `WorkspaceQuotaExceededError`,那个用户的 agent 进不了沙箱(run 本身照跑,沙箱类工具被 `ToolBlockedError` 挡下)。
20 MB 素材 × 500 轮就到顶。「重复下载」与「无限增长」是同一个病的两面,改址一起治。

**Interfaces:**
- Produces:`CACHE_DIRNAME = "cache"`;`CACHE_TTL_S = 24 * 3600`;
  `def cache_digest(url: str) -> str` → `hashlib.sha256(url.encode()).hexdigest()[:32]`。
- 目录形状:`agents/<key>/inputs/cache/<digest><ext>`(**与 `<run_id>/` 平级,不在它下面**)
  与 `agents/<key>/inputs/<run_id>/inputs.json`;`local_path` 指向 `inputs/cache/<digest><ext>`(仍相对 `/workspace`)。
- 只有一个时间戳:cache 文件的 **mtime = 最近一次下载时间**。命中时**不 touch**(台账 Ruling A):
  24h 内命中不重下、mtime 不动;超 24h 命中会重下、mtime 刷新。于是 mtime 就是「最近引用时间」的 24h 粒度近似,
  T12 的 7 天回收闸只会打到真的没人引用的条目。**不要加 `os.utime`**——加了热文件就永远不刷新内容,而 B-61 的起因正是一个换了的 logo。

- [ ] **Step 1: 写失败的测试**

沿用本文件已有的 `http_server` fixture(`(base, routes)` 两元组 + `routes` 可塞路由)。**每条新断言都要变异自证**。

```python
def test_cache_digest_is_stable_and_url_keyed() -> None:
    from orchestrator.tools.prefetch_script import cache_digest

    first = cache_digest("https://x/a.jpg")
    assert first == cache_digest("https://x/a.jpg")
    assert first != cache_digest("https://x/b.jpg")
    assert len(first) == 32
    assert "/" not in first and "." not in first


def test_second_fetch_of_the_same_url_reuses_the_cache_without_a_request(
    tmp_path: Path, http_server: _HttpServer
) -> None:
    # 同一 URL 第二次: 命中缓存, server 不该再收到请求, 预算不该被扣
    ...
    assert hits == [1, 1]          # server 端计数: 第二次没有新请求
    assert second_used == 0        # 命中不扣预算


def test_a_url_without_an_extension_still_hits_on_the_second_fetch(
    tmp_path: Path, http_server: _HttpServer
) -> None:
    """探针必须按 digest 前缀扫目录, 不能拼 URL 扩展名。

    ``/logo`` 这种没有扩展名的 URL, 响应 ``image/png`` 会落成 ``<digest>.png``;
    按 URL 扩展名去探就是探 ``<digest>``(空后缀)——永久不命中且每轮重下,
    这个任务等于白做。这条是该错法的实证。
    """
    ...


def test_an_expired_cache_entry_is_refetched(
    tmp_path: Path, http_server: _HttpServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 把已落盘的条目 mtime 拨老到 CACHE_TTL_S 之外 → 必须重新发请求
    ...


def test_a_cached_file_is_group_and_world_readable(
    tmp_path: Path, http_server: _HttpServer
) -> None:
    """落盘权限位必须是 0644。

    并发 run 撞同一 URL 要用唯一临时文件(``mkstemp``), 而 ``mkstemp`` 恒 0600、
    ``os.replace`` 保权限位 —— 不显式 chmod 就会把今天 ``open()`` 写出来的 0644
    静默降成 0600, 跨 uid 的读方(W2-BUG-1 的原病)再次读不到。
    """
    ...
    assert stat.S_IMODE(os.stat(cached).st_mode) == 0o644


def test_an_interrupted_write_never_leaves_a_partial_cache_entry(tmp_path: Path) -> None:
    # 同目录临时文件 + os.replace: 目标路径上要么是完整文件, 要么不存在
    ...
```

- [ ] **Step 2: 跑测试确认它红**

Run: `uv run --no-sync pytest services/orchestrator/tests/test_prefetch_script.py -k cache -v`
Expected: FAIL —— `cannot import name 'cache_digest'`

- [ ] **Step 3: 写实现**

`_fetch` 改三段:

① **先探缓存,一个请求都不发**。`cache_dir` 用 `os.scandir` 扫**文件名以 `<digest>` 开头**的条目
(digest 是 32 位 hex,不含 glob 元字符;不要拼 URL 扩展名去猜后缀,见 Step 1 的那条测试)。
新鲜(`time.time() - st_mtime < CACHE_TTL_S`)就返回它的相对路径、`used=0`。

**勘误(T11 评审 I-1)**:本段原写「取第一个条目,超期就算未命中」——**那是个 bug**。同一 URL 的后缀跨
24h 变了(`image/png`→`image/jpeg`;CDN 回 `octet-stream` 落到 URL 扩展名)会同时存在两个条目,而
`os.replace` 只覆盖同名,于是超期的兄弟条目**永久遮住**新鲜的那个:每轮重下、每轮多一份,正是本任务要治的病,
还因 scandir 是 FS 哈希序而不确定。正确写法:**超期条目跳过并顺手 `unlink`(失败不外抛),不要提前返回**,
扫完整个前缀集合 —— 这样清理与命中都与扫描顺序无关。

② 未命中(或已超期)才下载,判定顺序与今天完全一致(content-type → 声明长度 → 预算 → 实际长度比对)。

③ 落盘:`tempfile.mkstemp(dir=cache_dir)` 拿唯一临时文件(并发 run 撞同一 URL 也不会互相踩),写完
**`os.chmod(tmp, 0o644)`** 再 `os.replace` 到 `<digest><ext>`。后缀仍由响应的 content-type 定(回落 URL 扩展名)。
超期条目已在①被删掉,这里 `os.replace` 落的是新条目。

预算只扣真正下载的字节;命中不扣。`_fetch` 的签名去掉 `var_name` / `path`(内容寻址后文件名与变量无关),
`main` 里的 `rel_prefix` 改成 `posixpath.join("inputs", CACHE_DIRNAME)`,`files_dir` 改成
`os.path.join(os.path.dirname(run_dir), CACHE_DIRNAME)`——注意是 **run 目录的父目录**下的 `cache/`。

**`target_name` 连同它的两条测试一并删**(台账 Ruling C):内容寻址后没有调用点。它带的路径穿越校验不是丢了
而是结构上不再需要 —— 租户字符串根本不进文件名,只有 hash。

- [ ] **Step 4: 顺手改掉一条不准的注释**

`inputs_node.py` 的 `_PREFETCH_TIMEOUT_S` 注释写着超过 300 会「被截成 300」。实际是
`sandbox_supervisor/schemas.py:73` 的 `Field(..., gt=0, le=300)` —— 超了是 **422 拒绝**,不是截断。
把那半句改成「高于硬顶会被 supervisor 以 422 拒掉,不是被截断」。只改注释,不改值。

- [ ] **Step 5: 跑测试确认它绿 + 变异自证**

删掉命中分支 → `test_second_fetch_of_the_same_url_reuses_the_cache_without_a_request` 必须红;还原变绿。
把探针改成拼 URL 扩展名 → `test_a_url_without_an_extension_still_hits_on_the_second_fetch` 必须红;还原变绿。
去掉 `os.chmod` → 权限位那条必须红;还原变绿。
把 `os.replace` 换成直接写目标文件 → 分块写入被打断时能读到半截文件,对应测试必须红。
最后跑全量:`uv run --no-sync pytest services/orchestrator/tests/test_prefetch_script.py services/orchestrator/tests/test_inputs_node.py services/orchestrator/tests/test_inputs_doc.py -q`。

- [ ] **Step 6: 提交**

```bash
git add services/orchestrator/src/orchestrator/tools/prefetch_script.py \
        services/orchestrator/src/orchestrator/graph_builder/inputs_node.py \
        services/orchestrator/tests/test_prefetch_script.py
git commit -m "feat(inputs): 预拉改内容寻址缓存——同 URL 跨轮只下一次(B-61 T11)"
```

---

## Task 12: janitor 回收 agent 的 inputs 目录

**Files:**
- Modify: `services/control-plane/src/control_plane/workspace_janitor.py`
- Test: `services/control-plane/tests/test_workspace_janitor.py`(**加进既有文件**,复用它的 `_build` / `_age`;不新建测试文件)

**为什么**:现有 janitor 只扫 `_scratch/`(24h)、归档与体积记账,**没有任何东西碰 `agents/<key>/inputs/`**
(spec §4.6 勘误)。不回收就是只写不收的池子,最终撞配额闸。

**Interfaces:**
- Produces:`JanitorRunStats` 增加 `inputs_files_removed: int = 0` / `inputs_dirs_removed: int = 0`;
  新 phase `_sweep_agent_inputs`,在 `_run_cycle` 的 `for phase in (...)` 里插在 **`_sweep_sizes` 之前**
  (顺序:`_sweep_archives` → `_sweep_agent_inputs` → `_sweep_sizes` → `_sweep_scratch`)。
  —— 台账 Ruling F:`_sweep_sizes` 本来就逐用户 `refresh()` 跑全树 du,排在它前面回收,**同一轮记账天然反映回收量**;
  排到最后就得再 du 一遍全树。计划原文「删完刷一次体积记账」的诉求由顺序满足,不另加一次刷新。
- 常量:`WORKSPACE_AGENTS_DIR` 从 `expert_work.persistence.workspace.layout` import(它是共享包的公开名);
  `inputs` / `cache` 按本文件 `_SCRATCH_DIR` 的先例做模块私有常量 + 注释指向 orchestrator 侧的
  `tools/inputs_doc.py`、`tools/prefetch_script.py`(私名不跨包 import)。
- 树形:`<root>/<tenant>/<user>/agents/<agent_key>/inputs/{cache/, <run_id>/}`。tenant/user 两层复用既有
  `_list_uuid_dirs`;`agents/` 下是 agent key(**不是 UUID**),用 `os.scandir`。

- **策略表驱动,不要把 `inputs/` 写死在循环里**:

```python
@dataclass(frozen=True)
class _ReclaimPolicy:
    """一条回收策略。``enabled=False`` 的条目本批不生效,代码路径仍然走到。"""

    label: str            # 指标与日志用
    ttl_s: float
    enabled: bool


#: B-61 T12 本批只启用 inputs 两条;uploads 与产物的条目**先放在这里但关着**——
#: 它们删的是用户数据,需要产品定 N、需要发布前告知、还要对外删除端点(B-62)当自救
#: 出口,那是 B-63 的事。机制一次写好,B-63 落地时是把开关拨开 + 接 touch 点,不是重写。
_POLICIES = (
    _ReclaimPolicy(label="inputs_cache", ttl_s=7 * 24 * 3600, enabled=True),
    _ReclaimPolicy(label="inputs_run_dir", ttl_s=7 * 24 * 3600, enabled=True),
    _ReclaimPolicy(label="uploads", ttl_s=90 * 24 * 3600, enabled=False),
    _ReclaimPolicy(label="artifacts", ttl_s=90 * 24 * 3600, enabled=False),
)
```

**只做一条 TTL,不做水位驱逐**(2026-09-15 用户拍板):按最后引用时间过期这一条规则就够,先例都是这个形状
(OpenAI vector store「最后活跃后 7 天」、Codespaces「30 天,连一次重置」、浏览器 LRU)。水位 + LRU + 归档分层
先不做 —— 等真观测到「窗口内就把配额爆掉」再说。

**判据只看 mtime,不要碰 atime**:NFS 上 `atime` 基本不可信(多半 `noatime`/`relatime`)。
cache 条目的 mtime 由 T11 维持成「最近一次下载时间」——24h 内命中不重下、超期命中会重下刷新 mtime,
所以它就是「最近引用时间」的 24h 粒度近似。**T11 不会 touch,这里也不需要任何 touch 配合**(台账 Ruling A)。
`uploads`/产物的 touch 点(`read_document`、下载端点)属 B-63,本批不接。

**勘误(2026-09-15,T12 执行时核出)**:本条原写「清理范围要含 `inputs.json.tmp`」,依据是错的 —— `prefetch_script._rewrite` 写的是 `<inputs_path>.tmp`,即 `inputs/<run_id>/inputs.json.tmp`,**在 run 目录里面**,本来就随整个 run 目录一起回收,`inputs/` 这一层打不到它。子句保留但属防御性、今天不可达;**保留的理由是机制不是名字** —— `file_names` 非 None 这条规则是 `inputs/README.md` 这类文件不被误删的依据。真正会攒在 `cache/` 里的残留是 `mkstemp` 的 `tmpXXXXXXXX`,由缓存策略「该层所有文件」那条覆盖。

- [ ] **Step 1: 写失败的测试**

```python
async def test_sweep_removes_expired_cache_files_and_run_dirs(tmp_path: Path) -> None:
    ...


async def test_sweep_never_touches_a_recently_written_run_dir(tmp_path: Path) -> None:
    # 活跃 run 的目录 mtime 是新的, 必须留下
    # 这条是「删错会打断执行」的反向实证, 不是锦上添花
    ...


async def test_sweep_ignores_dirs_that_are_not_uuid_shaped(tmp_path: Path) -> None:
    # 只认 inputs/<uuid>/ 与 inputs/cache/, 其它一律不碰
    ...


async def test_reclaim_lands_in_the_same_cycle_size_accounting(tmp_path: Path) -> None:
    # 回收发生在 _sweep_sizes 之前: 同一轮 run_once 之后, 记下的体积必须已经是回收后的
    # 这条是 phase 顺序的实证 —— 把新 phase 挪到 _sweep_sizes 之后它就会红
    ...


async def test_disabled_policies_delete_nothing(tmp_path: Path) -> None:
    # uploads / 产物的条目本批是关着的: 造出超期的 uploads 文件, 扫完必须还在
    # 这条挡的是「以后有人顺手把 enabled 改成 True 就上线了」
    ...


async def test_a_leftover_tmp_file_is_reclaimed(tmp_path: Path) -> None:
    # T11 增量重写留下的 inputs.json.tmp, 被 kill 时会遗留
    ...
```

- [ ] **Step 2: 跑测试确认它红**

Run: `uv run --no-sync pytest services/control-plane/tests/test_workspace_janitor.py -k inputs -v`
Expected: FAIL —— `JanitorRunStats` 没有 `inputs_files_removed`

- [ ] **Step 3: 写实现**

按 `_POLICIES` 逐条执行(`enabled=False` 的跳过但**要走到判定**,别用 if 把整条路径短路掉,否则 B-63 开关拨开那天等于全新代码)。
启用的两条:遍历 `<user_root>/agents/*/inputs/`,`cache/` 下按 mtime 删过期文件;UUID 形状的子目录按 mtime 删整个目录
(目录 mtime 会被 T11 每次改写 `inputs.json` 顶上去,活跃 run 的目录因此总是新的)。`inputs/` 下直接躺着的
`inputs.json.tmp` 残留也按同一条 TTL 收掉。**只认 UUID 形状的目录名与 `cache`**,其它一律跳过(别把别人建的目录当垃圾)。
单目录失败 log + 继续,照本文件既有口径(`_list_uuid_dirs` / `_sweep_scratch` 都是这么写的),别让一个 ESTALE 带走整轮。
IO 一律 `await asyncio.to_thread(...)`,与既有 phase 同形。

- [ ] **Step 4: 跑测试确认它绿 + 变异自证**

把 mtime 判定改成无条件删 → `test_sweep_never_touches_a_recently_written_run_dir` 必须红;还原变绿。
把 `uploads` 那条的 `enabled` 改成 `True` → `test_disabled_policies_delete_nothing` 必须红;还原变绿。
把新 phase 挪到 `_sweep_sizes` 之后 → `test_reclaim_lands_in_the_same_cycle_size_accounting` 必须红;还原变绿。
最后跑全量:`uv run --no-sync pytest services/control-plane/tests/test_workspace_janitor.py -q`。

- [ ] **Step 5: 提交**

```bash
git add services/control-plane/src/control_plane/workspace_janitor.py \
        services/control-plane/tests/test_workspace_janitor.py
git commit -m "feat(janitor): 回收 agents/<key>/inputs 的过期缓存与 run 目录(B-61 T12)"
```

---

## 自审

**1. spec 覆盖**

| spec 节 | 由哪个 task 实现 |
|---|---|
| §4.1 位置与结构 | T1 |
| §4.2 单阶段时序 | T3 |
| §4.3 预拉规则(沙箱内、content-type、限额、降级、不绕过出网策略) | T2 + T3 + T10 Step 3 |
| §4.4 `EXPERT_WORK_INPUTS` 与工具描述 | T4 |
| §4.5 `trusted` 标记 | T1(文档里带标记)+ 现有围栏行为不动 |
| §4.6 清理 | 现有 janitor,无代码改动;T9 文档写明 7 天 |
| §5.1 manifest 字段 | T5 |
| §5.2 剥 schema | T6 + T7 |
| §5.3 填值点 | T6 + T7 |
| §5.4 四条错误语义 | T5(悬空/删变量/重复)+ T6(缺变量)+ T7(参数不存在) |
| §5.5 配置页 | T8 |
| §六 安全 | T2(下载在沙箱)+ T7(审计不记值)+ T10 |
| §七 存量/回滚 | T9 + T10 Step 5 |
| §八 可观测性 | T2(预拉 report)+ T7(审计列) |
| §九 测试 | T1-T8 各自的用例 + T10 |
| §十 PR 切分 | T1-T4 = PR-A;T5-T7 = PR-B;T8 = PR-C;T9 = PR-D |

**2. 占位符扫描**:无 TBD / TODO;每个代码步骤都给了可直接落的代码;T7、T8 的夹具明确要求扩展既有 conftest 而不是新造。

**3. 类型一致**:`inputs_rel_path` / `inputs_abs_path` 在 T1 定义、T3 与 T4 引用;`UrlSite` 的 `path` 在 T1(`tuple`)与 T2(`list`,脚本里 JSON 友好)形状不同但只在各自一侧用,T2 Step 5 的对拍测试把两者对齐;`apply_arg_bindings` 的返回 `(list, list[str])` 在 T6 定义、T7 消费;`PROMPT_INPUTS_KEY` 在 T3 定义、T7 消费;`bindings_by_tool` 的键是**裸工具名**,`ToolRegistry.arg_bindings()` 的键是**命名空间名**(`mcp__<server>__<tool>`)—— T7 实现里用 `mcp_tool_name()` 转换,两处别混。
