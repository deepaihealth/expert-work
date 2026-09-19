"""预装清单的漂移闸 —— B-82。

``SANDBOX_PREINSTALLED_PYTHON`` 进了 ``exec_python`` / ``bash`` 的工具描述,
模型会照它决定"要不要先装包"。所以它一旦跟镜像实际预装的东西分叉,代价不是
测试红,是模型被平台骗:少一条 → 它白装一遍已经有的包(在阿里云上一两分钟,
见 B-81);多一条 → 它直接 ``import`` 然后 ``ModuleNotFoundError``,而那是在
写完整段代码之后才炸。

手法照 ``test_image_env_matches_dockerfile``:刻意不打 ``@pytest.mark.
integration``、也刻意不 ``skip`` —— 漂移闸在跳过时等于不存在。
``requirements.txt`` 在仓库 checkout 里必然存在,真找不到说明目录结构变了,
那正该红。
"""

from __future__ import annotations

import re
from pathlib import Path

from orchestrator.tools.sandbox_image_contract import (
    SANDBOX_PREINSTALLED_BINARIES,
    SANDBOX_PREINSTALLED_PYTHON,
    preinstalled_note,
)

#: ``name==version`` 的顶层钉版行。``requirements.txt`` 头注释明写"Top-level
#: pins only",传递依赖由 pip 解析,所以这个形状就是全部。
_PIN = re.compile(r"^([A-Za-z0-9._-]+)\s*==")


def _requirements_path() -> Path:
    path = Path(__file__).resolve().parents[3] / "infra" / "sandbox-image" / "requirements.txt"
    assert path.is_file(), f"沙箱镜像 requirements.txt 不在预期位置:{path}"
    return path


def _pinned_names() -> list[str]:
    names: list[str] = []
    for raw in _requirements_path().read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _PIN.match(line)
        assert match is not None, (
            f"requirements.txt 出现了不是 ``name==version`` 的行:{line!r} —— "
            "这个解析器只认顶层钉版(见文件头注释);真要加别的形状,先改这里的闸。"
        )
        names.append(match.group(1))
    return names


def test_preinstalled_matches_requirements() -> None:
    """双向比对:少一条模型会白装,多一条模型会 import 失败。"""
    pinned = _pinned_names()

    assert list(SANDBOX_PREINSTALLED_PYTHON) == pinned, (
        "SANDBOX_PREINSTALLED_PYTHON 与 infra/sandbox-image/requirements.txt 已经分叉"
        f"(常量={list(SANDBOX_PREINSTALLED_PYTHON)} / 钉版={pinned})——这份清单进了"
        " exec_python/bash 的工具描述,分叉等于平台在骗模型。注意顺序也钉住:两边都按"
        " requirements.txt 的声明顺序,顺序是分组语义(表格类 / 文档类 / PDF 生成 / 图像)"
        "的一部分,乱序会让工具描述读起来像随机堆砌。"
    )


def test_manifest_is_not_empty_and_has_no_duplicates() -> None:
    """空清单会静默通过上面那条(两边都空),重复项会让工具描述变啰嗦。"""
    assert SANDBOX_PREINSTALLED_PYTHON, "预装清单不该是空的 —— 空了上面那条闸就退化成恒真"
    assert len(set(SANDBOX_PREINSTALLED_PYTHON)) == len(SANDBOX_PREINSTALLED_PYTHON), (
        f"预装清单有重复项:{SANDBOX_PREINSTALLED_PYTHON}"
    )


def _dockerfile_text() -> str:
    path = Path(__file__).resolve().parents[3] / "infra" / "sandbox-image" / "Dockerfile"
    assert path.is_file(), f"沙箱镜像 Dockerfile 不在预期位置:{path}"
    return path.read_text(encoding="utf-8")


def test_preinstalled_binaries_are_in_the_dockerfile() -> None:
    """单向闸:清单里列的命令行工具,对应的 apt 包必须真的装了。

    反向**刻意不钉** —— Dockerfile 的 apt 段里多数是字体、locale 和共享库
    (libpango / libgdk-pixbuf 之类),模型用不上也不该占工具描述的字符。
    """
    dockerfile = _dockerfile_text()
    for command, apt_package in SANDBOX_PREINSTALLED_BINARIES:
        assert re.search(rf"^\s*{re.escape(apt_package)}\s*\\?\s*$", dockerfile, re.MULTILINE), (
            f"工具描述里说沙箱有 {command!r},但 Dockerfile 的 apt 段里找不到 "
            f"{apt_package!r} —— 平台在骗模型:它会直接调用一个不存在的命令。"
        )


def test_note_names_every_entry() -> None:
    """渲染出来的那句话必须把两份清单都念全 —— 漏了的那条等于没加。"""
    note = preinstalled_note()
    for name in SANDBOX_PREINSTALLED_PYTHON:
        assert name in note, f"预装库 {name!r} 没有出现在工具描述那句话里"
    for command, _apt in SANDBOX_PREINSTALLED_BINARIES:
        assert command in note, f"预装命令 {command!r} 没有出现在工具描述那句话里"
