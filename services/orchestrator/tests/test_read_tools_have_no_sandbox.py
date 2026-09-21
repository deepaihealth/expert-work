"""只读文件工具**不许**依赖沙箱 —— 让 B-84 那个修法成立的不变式。

为什么这条要单独钉住:``list_dir`` 实测 60 天失败 **11%(22/207)**,那 22 次
**每一次**都是沙箱创建失败。一次失败的探测会让模型断定文件不存在,然后把
它自己上一轮写的文件重打一遍 —— 2026-09-16 的 run ``7fad305b`` 就是这么从
一次 ``sandbox create failed: 504`` 滚成了 1,178 秒(同样的输入在修好之后
重放三次是 177/235/254 秒)。

B-84 PR-2b 的修法是把这三个只读工具搬到宿主侧 NAS,于是「沙箱创建失败」在
这条路径上**没有发生通路**。但那个保证不写在任何断言里:它只是"当前的实现
恰好没有引用沙箱"。谁哪天为了别的理由给 ``ListDirTool`` 加回一个沙箱依赖,
保证会静默消失,而现有测试一条都不会红 —— 它们测的是"列目录列得对不对",
不是"列目录靠什么列"。

所以这里断言的是**依赖的形状**,不是行为。反向那一半同样要断言:写工具
**必须**仍然带着沙箱依赖(``write_file`` / ``edit_file`` 与 B-60 的私有
``/w`` 挂载空间和工作区写锁绑着,搬它是另一个量级的改动)。只禁不立的话,
把沙箱依赖改个名字就能绕过去。
"""

from __future__ import annotations

import dataclasses

import pytest
from orchestrator.tools.file_ops import (
    EditFileTool,
    ListDirTool,
    ReadFileTool,
    SearchFilesTool,
    WriteFileTool,
)

#: 字段名或字段类型里出现这些片段,就算带着沙箱依赖。
_SANDBOX_MARKERS = ("sandbox", "runtime", "supervisor", "e2b")


def _sandbox_fields(cls: type) -> list[str]:
    hits = []
    for field in dataclasses.fields(cls):
        haystack = f"{field.name} {field.type}".lower()
        if any(marker in haystack for marker in _SANDBOX_MARKERS):
            hits.append(field.name)
    return hits


@pytest.mark.parametrize("cls", [ReadFileTool, ListDirTool, SearchFilesTool])
def test_read_tools_carry_no_sandbox_dependency(cls: type) -> None:
    """只读工具的构造里不许出现沙箱 —— 不是"没用到",是拿不到。"""
    hits = _sandbox_fields(cls)
    assert not hits, (
        f"{cls.__name__} 又带上了沙箱依赖 {hits} —— B-84 PR-2b 把只读文件操作搬到宿主 NAS,"
        f"正是为了让「沙箱创建失败」在这条路径上没有发生通路(实测 list_dir 11% 失败,"
        f"22/22 次都是沙箱创建失败;一次失败会让模型重打整个文件)。"
        f"如果这个依赖是必要的,那 PR-2b 的保证就不再成立,请先改 "
        f"docs/superpowers/specs/2026-09-20-workspace-visibility-design.md 再改这里。"
    )


@pytest.mark.parametrize("cls", [WriteFileTool, EditFileTool])
def test_write_tools_still_carry_a_sandbox_dependency(cls: type) -> None:
    """反向的一半:写工具**必须**还在沙箱里。

    只禁不立的话,把 ``client: SandboxRuntime`` 改个名字就能绕过上面那条。
    这条同时钉住 B-84 的范围裁定:**只有读挪走了,写没有**。
    """
    hits = _sandbox_fields(cls)
    assert hits, (
        f"{cls.__name__} 不再带沙箱依赖了 —— B-84 只把**读**挪到了宿主侧,写操作与 B-60 的"
        f"私有 /w 挂载空间和工作区写锁绑着,没有挪。若真要挪写操作,那是独立一项设计,"
        f"不该由这条测试的失败来宣布。"
    )
