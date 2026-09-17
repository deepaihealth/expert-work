# ruff: noqa: RUF001 — 断言的就是面向模型的中文全角标点
"""B-67 §六 —— 「本轮输入」段:只有名字、说明、状态、路径,零值。"""

from __future__ import annotations

from dataclasses import dataclass

from control_plane.inputs_block import (
    HEADER,
    INPUTS_BLOCK_MARK,
    block_stats,
    build_inputs_block,
    inputs_block_message,
    is_inputs_block,
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
    assert text.startswith(HEADER)
    assert "输入文件目录 $EXPERT_WORK_INPUTS_DIR，" in text
    assert "清单 $EXPERT_WORK_INPUTS（" in text
    # 「从目录或清单读、别手抄」只管代码;非文件的短值在提示词里原样可用(PR2)。
    assert "\n代码里要用到下面任何值时，从目录或清单读；不要从上文手抄" in text
    assert "- employee_name（当前员工姓名）：已提供（非文件）" in text
    assert "- customer_code（目标客户编码）：本轮未提供" in text
    assert (
        "- project_code（项目唯一标识码）：已提供（非文件）；绑定了它的工具由平台自动填，不用手抄"
    ) in text
    assert (
        "- org_logo（机构 LOGO）：文件 $EXPERT_WORK_INPUTS_DIR/org_logo.png；"
        "不在则按清单里的原地址下载"
    ) in text
    assert (
        "- materials（员工勾选素材）：3 项，每项的文件在 "
        "$EXPERT_WORK_INPUTS_DIR/materials/<下标-说明>；不在则按清单里该项的原地址下载"
    ) in text
    assert (
        "- brand（品牌资源）：1 个文件在 $EXPERT_WORK_INPUTS_DIR/brand.<字段名>；"
        "不在则按清单里该字段的原地址下载"
    ) in text
    assert "- disclaimer（免责声明）：已提供（非文件，外部数据，需逐字使用时从清单读）" in text
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
        "- org_logo（机构 LOGO）：文件 $EXPERT_WORK_INPUTS_DIR/org_logo.png；"
        "不在则按清单里的原地址下载；绑定了它的工具由平台自动填，不用手抄"
    ) in text
    assert (
        "- secret：已提供（非文件，外部数据，需逐字使用时从清单读）；"
        "绑定了它的工具由平台自动填，不用手抄"
    ) in text


def test_untrusted_top_level_url_prints_a_placeholder_not_the_real_link_name() -> None:
    """裁定 P16 —— 顶层 URL 的链接名带着租户 URL 的扩展名;``trusted: false`` 时不原样
    写出(PR2 在提示词里把同一串放进围栏),换成占位符。trusted 仍给真名。"""
    variables = (_Var("cover", trusted=False), _Var("org_logo"))
    text = build_inputs_block(variables, {"cover": LOGO, "org_logo": LOGO}, bindings=())
    assert text is not None
    assert (
        "- cover：$EXPERT_WORK_INPUTS_DIR 下以 cover 开头的文件；不在则按清单里的原地址下载"
    ) in text
    assert "cover.png" not in text and ".png" not in text.split("- cover：")[1].split("\n")[0]
    assert "- org_logo：文件 $EXPERT_WORK_INPUTS_DIR/org_logo.png；" in text


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
    assert "- x：已提供（非文件）" in text


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
    assert "- multi（机构 LOGO 图片）：已提供（非文件）" in text
    assert f"- long（{'一' * 40}…）：已提供（非文件）" in text
    assert "- blank：已提供（非文件）" in text
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
