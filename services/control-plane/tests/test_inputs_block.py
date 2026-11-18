# ruff: noqa: RUF001 — 断言的就是面向模型的中文全角标点
"""B-67 §六 —— 「本轮输入」段:只有名字、说明、状态、路径,零值。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

import pytest

from control_plane.inputs_block import (
    FILES_FACT,
    HEADER,
    INPUTS_BLOCK_MARK,
    block_stats,
    build_inputs_block,
    inputs_block_message,
    is_inputs_block,
    reads_files_from_catalog,
)
from expert_work.common.conversation_channel import is_hidden
from orchestrator.tools.inputs_doc import linked_sites


@dataclass
class _Var:
    name: str
    trusted: bool = True
    required: bool = True
    description: str | None = None


LOGO = "https://files.example.com/brand/cover-1726394851207.png"
VARS = (
    _Var("employee_name", description="当前员工姓名"),
    _Var("customer_code", required=False, description="目标客户编码"),
    _Var("project_code", description="项目唯一标识码"),
    _Var("org_logo", description="机构 LOGO"),
    _Var("materials", trusted=False, description="员工勾选素材"),
    _Var("brand", description="品牌资源"),
    _Var("disclaimer", trusted=False, description="免责声明"),
)
INPUTS = {
    "employee_name": "张三",
    "project_code": "PRJ001",
    "org_logo": LOGO,
    "materials": (
        '[{"description":"示范视频","url":"https://x/a.mp4"},'
        '{"description":"参考","url":"https://x/b.pdf"},{"description":"无链接"}]'
    ),
    "brand": {"logo": "https://x/l.jpg", "name": "示例机构"},
    "disclaimer": "本方案不构成医疗建议",
}


def test_block_lists_every_declared_variable_with_status_and_never_a_value() -> None:
    text = build_inputs_block(VARS, INPUTS, bindings={"project_code"})
    assert text is not None
    assert text.splitlines() == [
        HEADER,
        "本轮输入文件在 inputs/current/ 下（预先下载失败的不在）。",
        "- employee_name（当前员工姓名）：已提供",
        "- customer_code（目标客户编码）：本轮未提供",
        "- project_code（项目唯一标识码）：已提供；由平台填进绑定它的工具参数",
        "- org_logo（机构 LOGO）：文件 inputs/current/org_logo.png",
        "- materials（员工勾选素材）：3 项，其中 2 个文件在 inputs/current/materials/ 下"
        + "（按 <下标-说明> 命名）",
        "- brand（品牌资源）：1 个文件在 inputs/current/ 下（按 brand.<字段名> 命名）",
        "- disclaimer（免责声明）：已提供（外部数据）",
    ]
    for value in (
        "张三",
        "PRJ001",
        "1726394851207",
        "https://",
        "示范视频",
        "医疗建议",
        "示例机构",
    ):
        assert value not in text
    # 顶层 URL 的名字与预拉 / 渲染同源。
    assert linked_sites("org_logo", LOGO)[0].link == "org_logo.png"


def test_bound_variable_reports_its_shape_then_the_binding() -> None:
    """裁定 P10 —— 模板里被绑定的变量照形态渲染,绑定只在这里说一次:形态状态照常,
    后面接一句「绑定了它的工具由平台自动填」。"""
    variables = (_Var("org_logo", description="机构 LOGO"), _Var("secret", trusted=False))
    text = build_inputs_block(
        variables, {"org_logo": LOGO, "secret": "v"}, bindings={"org_logo", "secret"}
    )
    assert text is not None
    assert (
        "- org_logo（机构 LOGO）：文件 inputs/current/org_logo.png；由平台填进绑定它的工具参数"
    ) in text
    assert "- secret：已提供（外部数据）；由平台填进绑定它的工具参数" in text


def test_untrusted_top_level_url_prints_a_placeholder_not_the_real_link_name() -> None:
    """裁定 P16 —— 顶层 URL 的链接名带着租户 URL 的扩展名;``trusted: false`` 时不原样
    写出(PR2 在提示词里把同一串放进围栏),换成占位符。trusted 仍给真名。"""
    variables = (_Var("cover", trusted=False), _Var("org_logo"))
    text = build_inputs_block(variables, {"cover": LOGO, "org_logo": LOGO}, bindings=())
    assert text is not None
    assert (
        "- cover：文件在 inputs/current/ 下，名字以 cover 开头"
        "（确切文件名见清单里 cover 的 local_path）"
    ) in text
    assert "cover.png" not in text and ".png" not in text.split("- cover：")[1].split("\n")[0]
    assert "- org_logo：文件 inputs/current/org_logo.png\n" in text + "\n"


def test_an_untrusted_prefix_points_at_the_manifest_because_prefixes_collide() -> None:
    """B-73 ④ —— ``org`` 与 ``org_logo`` 都声明时,「以 org 开头的文件」把两个文件
    都框了进去。精确名平台知道但 untrusted 不能写进提示词,所以指向清单里那个变量
    自己的 ``local_path``(清单是沙箱读的文件,不是提示词)。"""
    variables = (_Var("org", trusted=False), _Var("org_logo", trusted=False))
    text = build_inputs_block(variables, {"org": LOGO, "org_logo": LOGO}, bindings=())
    assert text is not None
    org_line = text.split("- org：")[1].split("\n")[0]
    # 歧义还在(前缀就是会撞),但每一行都说了怎么从歧义里解出精确名。
    assert "名字以 org 开头" in org_line
    assert "见清单里 org 的 local_path" in org_line
    assert "见清单里 org_logo 的 local_path" in text.split("- org_logo：")[1].split("\n")[0]


def test_bound_optional_variable_not_passed_reports_unset_only() -> None:
    """裁定 P12 —— 先判「本轮未提供」:可选变量被绑定但本轮没传,只说没提供。"""
    text = build_inputs_block(
        (_Var("customer_code", required=False, description="目标客户编码"),),
        {},
        bindings={"customer_code"},
    )
    assert text is not None
    lines = [ln for ln in text.splitlines() if ln.startswith("- customer_code")]
    assert lines == ["- customer_code（目标客户编码）：本轮未提供"]


def test_variable_without_description_shows_the_name_only() -> None:
    text = build_inputs_block((_Var("x"),), {"x": "1"}, bindings=())
    assert text is not None
    assert "- x：已提供" in text


def test_description_is_one_line_and_capped() -> None:
    """说明是自由文本:空白(含换行)压成一个空格,超过 40 字截断并加「…」。"""
    long_desc = "一" * 45
    text = build_inputs_block(
        (
            _Var("multi", description="  机构\n  LOGO\t图片  "),
            _Var("long", description=long_desc),
            _Var("blank", description=" \n "),
        ),
        {"multi": "1", "long": "1", "blank": "1"},
        bindings=(),
    )
    assert text is not None
    assert "- multi（机构 LOGO 图片）：已提供" in text
    assert f"- long（{'一' * 40}…）：已提供" in text
    assert "- blank：已提供" in text
    assert "\n  LOGO" not in text


def test_no_variables_means_no_block() -> None:
    assert build_inputs_block((), {}, bindings=()) is None


def test_block_stats_count_names_not_values() -> None:
    assert block_stats(VARS, INPUTS, bindings={"project_code"}) == {
        "variable_count": 7,
        "bound_count": 1,
        "url_count": 4,
    }


def test_block_message_is_hidden_and_marked() -> None:
    msg = inputs_block_message("body")
    assert is_hidden(msg)
    assert is_inputs_block(msg)
    assert msg.additional_kwargs[INPUTS_BLOCK_MARK] is True
    assert msg.content == "body"


# --- B-317 —— 段里只陈述事实,不下命令 ------------------------------------------

#: 改动前段里出现过的命令与旧写法。任何一个回到段里,纯对话的智能体就又会被引去跑代码读值。
_COMMANDS = (
    "exec_python",
    "bash",
    "从目录或清单读",
    "不要从上文手抄",
    "不用手抄",
    "原地址下载",
    "$EXPERT_WORK",
    "EXPERT_WORK_INPUTS",
)

_CORPUS_VARS = (
    *VARS,
    _Var("cover", trusted=False),
    _Var("signed", description="签名地址"),
    _Var("blob", trusted=False),
)
_CORPUS = {
    **INPUTS,
    "cover": "https://cdn.example.com/u/7f3a9c2e/cover-final.jpeg?sig=abcDEF123456",
    "signed": "https://oss.example.com/a/report.pdf?Expires=1999999999&Signature=QWERTYUIOP",
    "blob": '{"token":"sk-live-0123456789abcdef","note":"客户自述胸闷三天"}',
}


@pytest.mark.parametrize("reads_files", [True, False])
@pytest.mark.parametrize("bound", [frozenset(), frozenset(v.name for v in _CORPUS_VARS)])
def test_block_never_carries_a_command_or_the_env_spelling(
    reads_files: bool, bound: frozenset[str]
) -> None:
    for inputs in (_CORPUS, {}, {"employee_name": "张三"}):
        text = build_inputs_block(_CORPUS_VARS, inputs, bindings=bound, reads_files=reads_files)
        assert text is not None
        for command in _COMMANDS:
            assert command not in text, command


def test_files_fact_only_when_some_variable_has_a_file() -> None:
    only_text = build_inputs_block(VARS, {"employee_name": "张三"}, bindings=())
    assert only_text is not None
    assert FILES_FACT not in only_text
    with_file = build_inputs_block(VARS, {"org_logo": LOGO}, bindings=())
    assert with_file is not None
    assert with_file.count(FILES_FACT) == 1
    assert with_file.splitlines()[1] == FILES_FACT
    # 绑定的裸字符串数组(健管师 attachment_urls):平台不预拉,不算文件。
    bare = build_inputs_block(
        (_Var("attachment_urls"),), {"attachment_urls": ["https://x/a.jpg"]}, bindings=()
    )
    assert bare is not None
    assert FILES_FACT not in bare
    assert "- attachment_urls：已提供" in bare


def test_file_lines_never_carry_a_run_id() -> None:
    text = build_inputs_block(_CORPUS_VARS, _CORPUS, bindings=())
    assert text is not None
    assert not re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-", text)


def test_no_value_substring_of_eight_chars_reaches_the_block() -> None:
    """零值:任何值(含由值推出的说明 slug、字典键之外的文本)里 8 字以上的片段都不出现。"""
    text = build_inputs_block(_CORPUS_VARS, _CORPUS, bindings=())
    assert text is not None
    for name, value in _CORPUS.items():
        raw = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        for start in range(len(raw) - 7):
            piece = raw[start : start + 8]
            assert piece not in text, (name, piece)


def test_untrusted_text_is_a_fact_without_pointing_at_the_manifest() -> None:
    text = build_inputs_block((_Var("d", trusted=False),), {"d": "x"}, bindings=())
    assert text is not None
    assert text.splitlines()[-1] == "- d：已提供（外部数据）"
    assert "清单" not in text


def test_without_a_file_reading_tool_no_paths_are_written() -> None:
    """没有任何读文件工具的智能体:不写文件句,文件变量也只说「已提供」(跟着能力走)。"""
    text = build_inputs_block(VARS, INPUTS, bindings={"org_logo"}, reads_files=False)
    assert text is not None
    assert FILES_FACT not in text
    assert "inputs/current" not in text
    assert "- org_logo（机构 LOGO）：已提供；由平台填进绑定它的工具参数" in text
    assert "- materials（员工勾选素材）：已提供（外部数据）" in text


def test_reads_files_follows_the_tool_catalog() -> None:
    assert reads_files_from_catalog(["mcp__shenhu__form_save", "exec_python"]) is True
    assert reads_files_from_catalog(["ask_image"]) is True
    assert reads_files_from_catalog(["mcp__shenhu__form_save", "web_search"]) is False
