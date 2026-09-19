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
    SANDBOX_UNAVAILABLE_NOTE,
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
    """单向闸:清单里列的命令行工具,必须真的在镜像里 —— 按**来源**分别验。

    反向**刻意不钉** —— Dockerfile 的 apt 段里多数是字体、locale 和共享库
    (libpango / libgdk-pixbuf 之类),模型用不上也不该占工具描述的字符。

    ``source`` 的两个取值在这里各有各的验法,而且 ``else`` 是 ``wheel`` 而不是
    "其它都放过":新增第三种来源会掉进 ``wheel`` 分支并且几乎肯定红,这正是想要
    的 —— 加来源的人必须回来补一条验法,不能静默滑过去(B-55)。
    """
    dockerfile = _dockerfile_text()
    pinned = _pinned_names()
    for entry in SANDBOX_PREINSTALLED_BINARIES:
        if entry.source == "apt":
            assert re.search(
                rf"^\s*{re.escape(entry.package)}\s*\\?\s*$", dockerfile, re.MULTILINE
            ), (
                f"工具描述里说沙箱有 {entry.command!r},但 Dockerfile 的 apt 段里找不到 "
                f"{entry.package!r} —— 平台在骗模型:它会直接调用一个不存在的命令。"
            )
            continue
        # wheel:二进制是 pip 包带来的,要两头都成立才算数 —— 包装了(否则
        # symlink 的源不存在),而且 symlink 真的建到了 PATH 上(否则模型敲
        # 裸名字仍然 command not found,哪怕文件就在 site-packages 里)。
        assert entry.package in pinned, (
            f"工具描述里说沙箱有 {entry.command!r},来源标的是 wheel {entry.package!r},"
            f"但 requirements.txt 里没有它 —— symlink 的源不存在。"
        )
        # ``re.DOTALL``:那条 ``RUN ln -s`` 是续行写的,源路径占一整行、目标在
        # 下一行。不带 DOTALL 的 ``.`` 不跨换行,闸会对一个**正确的** Dockerfile
        # 报红(写这条时第一版就是这么红的)。
        assert re.search(
            rf"ln -s .*?/usr/local/bin/{re.escape(entry.command)}\b", dockerfile, re.DOTALL
        ), (
            f"{entry.package!r} 装了,但 Dockerfile 没把它 symlink 成 "
            f"/usr/local/bin/{entry.command} —— 模型敲 {entry.command!r} 仍然是 "
            f"command not found。"
        )


def test_note_states_that_npm_is_gone() -> None:
    """否定句必须真的出现在工具描述里 —— B-55。

    这一条不是措辞洁癖:``docx`` / ``pptx`` 两个平台技能的正文明写
    ``npm install -g``,而 npm 已经不在镜像里了。肯定清单只说"有什么",不说
    "没什么";只摘掉 ``npm`` 那一条,模型照技能正文去敲,仍然白跑一轮。
    """
    note = preinstalled_note()
    assert "npm" in note, "工具描述没提 npm —— 技能正文还在教模型用它"
    assert SANDBOX_UNAVAILABLE_NOTE in note
    assert not any(entry.command == "npm" for entry in SANDBOX_PREINSTALLED_BINARIES), (
        "npm 又回到肯定清单里了 —— 镜像里没有它(B-55),这会让工具描述自相矛盾。"
    )


def test_note_names_every_entry() -> None:
    """渲染出来的那句话必须把两份清单都念全 —— 漏了的那条等于没加。"""
    note = preinstalled_note()
    for name in SANDBOX_PREINSTALLED_PYTHON:
        assert name in note, f"预装库 {name!r} 没有出现在工具描述那句话里"
    for entry in SANDBOX_PREINSTALLED_BINARIES:
        assert entry.command in note, f"预装命令 {entry.command!r} 没有出现在工具描述那句话里"
