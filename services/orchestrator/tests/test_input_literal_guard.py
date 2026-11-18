# ruff: noqa: RUF001 — 断言的就是面向模型的中文全角标点
"""B-317 —— 字面量守卫的判定与提示(纯函数部分)。"""

from __future__ import annotations

from typing import Any

import pytest

from orchestrator.graph_builder.input_literal_guard import (
    PATH_ARGS,
    LiteralHit,
    find_literal,
    literal_guard_message,
)

NAMES = frozenset({"customer_code", "attachment_urls", "org_logo", "price"})
MCP = "mcp__shenhu__form_save_customer_values"
RUN = "0f1e2d3c-4b5a-4968-8776-655443322110"


def _hit(tool: str, args: dict[str, Any], **kw: Any) -> LiteralHit | None:
    return find_literal(tool, args, names=NAMES, **kw)


# --- 受检面 ①:MCP / http 的全部字符串叶子 -----------------------------------------------


@pytest.mark.parametrize("tool", [MCP, "http"])
@pytest.mark.parametrize(
    "value",
    [
        "$attachment_urls",
        "附件:$attachment_urls",
        "${attachment_urls}",
        ["ok", {"nested": "x $attachment_urls y"}],
    ],
)
def test_a_declared_variable_after_a_dollar_is_caught_anywhere_in_a_remote_call(
    tool: str, value: Any
) -> None:
    hit = _hit(tool, {"note": "fine", "fields": value})
    assert hit is not None
    assert (hit.arg, hit.kind, hit.name, hit.remote) == (
        "fields",
        "variable",
        "attachment_urls",
        True,
    )


@pytest.mark.parametrize(
    "value",
    [
        "$100",
        "$5.99",
        "US$20",
        "$ 100",
        "$USD 100",
        "$customer",
        "$customer_code_v2",
        "$Customer_code",
        "${customer_code",
        "customer_code",
        "{{ customer_code }}",
        "inputs/outputs 比例",
        "见 inputs/current/a.png",
        "file:///workspace/inputs/current/a.png",
        "![](inputs/current/a.png)",
        '{"img": "inputs/current/a.png"}',
        "Bearer $TOKEN",
    ],
)
def test_remote_text_that_is_not_a_literal_passes(value: str) -> None:
    assert _hit(MCP, {"text": value}) is None
    assert _hit("http", {"body": value}) is None


@pytest.mark.parametrize(
    "value",
    [
        "inputs/current/a.png",
        "  inputs/current/a.png ",
        f"/workspace/inputs/{RUN}/a.png",
        f"inputs/{RUN}/m/0-x.mp4",
    ],
)
def test_a_whole_local_input_path_sent_to_a_remote_service_is_caught(value: str) -> None:
    hit = _hit(MCP, {"attachments": [value]}, inputs_run=RUN)
    assert hit is not None
    assert (hit.kind, hit.arg, hit.name) == ("local_path", "attachments", "")


@pytest.mark.parametrize(
    "value",
    [
        "inputs/cache/train.json",
        "inputs/cache/" + "a" * 32 + ".png",
        "inputs/1F1E2D3C-4B5A-4968-8776-655443322111/a.png",  # 不是规范写法(大写)
        "inputs/1f1e2d3c4b5a49688776655443322111/a.png",  # 不是规范写法(无连字符)
        "inputs/data.csv",
    ],
)
def test_other_inputs_paths_are_the_remote_services_own(value: str) -> None:
    """L1 —— 仓库类 MCP 的 ``get_file_contents(path="inputs/cache/train.json")`` 是对方自己的
    路径:只认 ``inputs/current/`` 与 ``inputs/<规范 UUID>/``。"""
    assert _hit("mcp__github__get_file_contents", {"path": value}, inputs_run=RUN) is None


OTHER_RUN = "1f1e2d3c-4b5a-4968-8776-655443322111"


@pytest.mark.parametrize(
    "value",
    [
        f"inputs/{OTHER_RUN}/a.png",
        f"/workspace/inputs/{OTHER_RUN}/m/0",
        # 第四轮 LOW-5 —— 相对写法 ``./`` 与 agent 根写法同样是本地路径。
        "./inputs/current/a.png",
        f"./inputs/{OTHER_RUN}/a.png",
        f"/agent/inputs/{OTHER_RUN}/a.png",
    ],
)
def test_a_previous_turns_input_path_sent_to_a_remote_service_is_caught(value: str) -> None:
    """复审 L2 —— 之后的轮次模型会照回执用 ``inputs/<上一轮 run>/x``,同样不能交给外部服务。"""
    hit = _hit(MCP, {"attachments": [value]}, inputs_run=RUN)
    assert hit is not None and hit.kind == "local_path"


@pytest.mark.parametrize("value", ["inputs/current/a.png", f"inputs/{OTHER_RUN}/a.png"])
def test_an_agent_that_declares_variables_is_checked_in_a_turn_without_inputs(value: str) -> None:
    """复审 L2 —— 本轮没传输入、但智能体声明了变量:前几轮的输入文件照样是本地路径。"""
    hit = _hit(MCP, {"path": value})
    assert hit is not None and hit.kind == "local_path"


@pytest.mark.parametrize("value", ["inputs/current/a.png", f"inputs/{RUN}/a.png"])
def test_without_inputs_or_variables_local_paths_are_not_checked(value: str) -> None:
    """L1 —— 没有输入也没声明变量的智能体不查规则 3。"""
    hit = find_literal("mcp__github__get_file_contents", {"path": value}, names=frozenset())
    assert hit is None


def test_env_names_are_checked_even_without_inputs() -> None:
    """规则 2 不看本轮有没有输入:``$EXPERT_WORK_`` 前缀归平台,外部服务里没有合法的字面用途。"""
    hit = _hit(MCP, {"a": "$EXPERT_WORK_SKILLS_DIR/x"})
    assert hit is not None and hit.kind == "env"


@pytest.mark.parametrize(
    "env",
    [
        "$EXPERT_WORK_INPUTS_DIR/x.png",
        "${EXPERT_WORK_INPUTS}",
        "  $EXPERT_WORK_INPUTS_DIR/x.png ",
        "file=$EXPERT_WORK_INPUTS_DIR/x.png",
        "./$EXPERT_WORK_SKILLS_DIR/a",
    ],
)
def test_platform_env_names_in_a_remote_call_are_caught(env: str) -> None:
    hit = _hit(MCP, {"a": env})
    assert hit is not None and hit.kind == "env"


@pytest.mark.parametrize(
    "text",
    [
        "#!/bin/sh\npython $EXPERT_WORK_SKILLS_DIR/pptx/gen.py out.pptx\n",
        "运行 $EXPERT_WORK_SKILLS_DIR/x/run.sh 之后再看结果",
        "---\nname: report\n---\n读 ${EXPERT_WORK_SKILLS_DIR}/report/SKILL.md",
    ],
)
def test_long_remote_text_that_mentions_a_platform_env_name_passes(text: str) -> None:
    """复审 L3 —— 交给外部服务的脚本、SKILL.md 正文里提到 ``$EXPERT_WORK_SKILLS_DIR`` 是正文,
    不是被当成路径的那串字;只有整段像路径(以它开头,或整段没有空白)才拦。"""
    assert _hit(MCP, {"body": text}) is None
    assert _hit("http", {"body": {"content": text}}) is None


@pytest.mark.parametrize(
    ("text", "name"),
    [
        ("请查看 $EXPERT_WORK_INPUTS_DIR/report.pdf", "EXPERT_WORK_INPUTS_DIR"),
        ("清单在 $EXPERT_WORK_INPUTS 里", "EXPERT_WORK_INPUTS"),
        ("见 ${EXPERT_WORK_INPUTS_DIR}/m/0-a.mp4 这个视频", "EXPERT_WORK_INPUTS_DIR"),
    ],
)
def test_only_the_skills_dir_is_let_through_in_long_remote_text(text: str, name: str) -> None:
    """第四轮 LOW-4 —— 长正文的放宽只给 ``$EXPERT_WORK_SKILLS_DIR``(技能脚本、SKILL.md 正文
    里的合法写法);别的 ``$EXPERT_WORK_*`` 出现在发给对方的正文里,对方收到的就是这串字,照拦。"""
    hit = _hit("mcp__wecom__send_message", {"content": text})
    assert hit is not None and (hit.kind, hit.name) == ("env", name)


def test_long_text_in_a_platform_path_argument_is_still_caught() -> None:
    """复审 L3 —— 路径参数不放宽:那里没有「正文」。"""
    hit = _hit("read_file", {"path": "notes $EXPERT_WORK_SKILLS_DIR/a.md"})
    assert hit is not None and hit.kind == "env"


def test_a_declared_variable_in_long_remote_text_is_still_caught() -> None:
    """复审 L3 只放宽 ``$EXPERT_WORK_*``;声明过的 ``$变量名`` 在正文里照样拦。"""
    hit = _hit(MCP, {"body": "附件见 $attachment_urls ,请查收"})
    assert hit is not None and hit.kind == "variable"


def test_bound_parameters_are_not_checked() -> None:
    """G6 —— 平台填进去的值是租户数据,里面有什么字样都不是模型写的。"""
    args = {"attachments": ["$customer_code", "inputs/current/x"], "summary": "ok"}
    assert _hit(MCP, args, skip_args={"attachments"}) is None
    assert _hit(MCP, args) is not None


def test_a_variable_bound_to_this_tool_names_the_bound_parameter() -> None:
    hit = _hit(
        MCP, {"summary": "$attachment_urls"}, bound_params={"attachment_urls": "attachments"}
    )
    assert hit is not None and hit.bound_param == "attachments"
    assert literal_guard_message(hit) == (
        "[blocked] 参数 summary 里的 $attachment_urls 不会被换成值，收到的就是这串字。"
        "attachment_urls 由平台自动填进本工具的 attachments 参数（它不在你看到的参数表里），"
        "这里不用写，去掉后重新调用。"
    )


# --- 受检面 ②③:平台工具的路径参数(本模块自己的表,不是 spec.path_args) -----------------


def test_the_path_table_is_explicit() -> None:
    assert PATH_ARGS["save_artifact"] == ("path",)
    assert PATH_ARGS["ask_image"] == ("path", "image_ref")
    assert "search_files" not in PATH_ARGS


def test_save_artifact_checks_path_not_name() -> None:
    """G4b —— ``save_artifact`` 的 ``spec.path_args`` 是产物名 ``name``(调度冲突键)。"""
    assert _hit("save_artifact", {"name": "$org_logo", "path": "out/a.md"}) is None
    hit = _hit("save_artifact", {"name": "a", "path": "$org_logo"})
    assert hit is not None and (hit.arg, hit.remote) == ("path", False)


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("exec_python", {"code": "print('$customer_code', '$EXPERT_WORK_INPUTS_DIR')"}),
        ("bash", {"command": "cp $EXPERT_WORK_INPUTS_DIR/x.png . && echo $org_logo"}),
        ("spawn_worker", {"task": "读 $EXPERT_WORK_SKILLS_DIR/pptx/SKILL.md,用 $org_logo"}),
        ("subagent", {"task": "$customer_code"}),
        ("update_plan", {"plan": "$EXPERT_WORK_INPUTS_DIR"}),
        ("author_skill", {"body": "run $EXPERT_WORK_SKILLS_DIR/x/gen.py"}),
        ("refine_skill", {"body": "$EXPERT_WORK_SKILLS_DIR"}),
        ("fork_skill", {"body": "$EXPERT_WORK_SKILLS_DIR"}),
        ("remember", {"text": "$customer_code"}),
        ("note_behavior_patch", {"note": "$customer_code"}),
        ("knowledge_search", {"query": "$customer_code"}),
        ("web_search", {"query": "$price"}),
        ("write_file", {"path": "run.sh", "content": "cp $EXPERT_WORK_INPUTS_DIR/x . $org_logo"}),
        (
            "edit_file",
            {
                "path": "run.sh",
                "old_string": "$org_logo",
                "new_string": "$EXPERT_WORK_SKILLS_DIR",
                "edits": [{"old_string": "$org_logo", "new_string": "$customer_code"}],
            },
        ),
    ],
)
def test_tools_outside_the_checked_surface_are_never_checked(
    tool: str, args: dict[str, Any]
) -> None:
    """G4 / G5 —— 白名单之外一律不查(平台技能教的 ``$EXPERT_WORK_SKILLS_DIR/...`` 写法有效)。"""
    assert _hit(tool, args) is None


def test_a_file_tools_path_is_checked_even_when_its_content_is_not() -> None:
    hit = _hit("write_file", {"path": "$org_logo", "content": "$org_logo"})
    assert hit is not None and hit.arg == "path"


@pytest.mark.parametrize(
    "path", ["$EXPERT_WORK_INPUTS_DIR/x.png", "${EXPERT_WORK_INPUTS_DIR}/m/0-a.mp4"]
)
def test_the_inputs_dir_alias_at_the_start_of_a_path_is_left_to_the_resolver(path: str) -> None:
    for written in (path, f"  {path}", f"\t{path} "):
        # L2 —— 与 ``resolve_workspace_path`` 同一个口径:先去首尾空白再认别名。
        assert _hit("ask_image", {"path": written}) is None
        assert _hit("read_document", {"path": written}) is None


@pytest.mark.parametrize(
    "path",
    [
        "a/$EXPERT_WORK_INPUTS_DIR/x.png",
        "$EXPERT_WORK_INPUTS",
        "$EXPERT_WORK_INPUTS_DIR",
        "$EXPERT_WORK_INPUTS_DIR/$org_logo",
    ],
)
def test_other_env_or_variable_uses_in_a_path_are_caught(path: str) -> None:
    assert _hit("read_file", {"path": path}) is not None


def test_a_local_path_is_fine_for_a_platform_tool() -> None:
    assert _hit("ask_image", {"path": "inputs/current/x.png"}) is None
    assert _hit("ask_image", {"image_ref": "inputs/current/x.png"}) is None


@pytest.mark.parametrize(
    ("ref", "kind"),
    [("$org_logo", "variable"), ("$EXPERT_WORK_SKILLS_DIR/a.png", "env")],
)
def test_ask_image_image_ref_is_checked_like_its_path(ref: str, kind: str) -> None:
    """复审 L6 —— ``image_ref`` 填工作区路径时按 ``path`` 处理(B-250),守卫也一样查。"""
    hit = _hit("ask_image", {"image_ref": ref, "question": "?"})
    assert hit is not None and (hit.arg, hit.kind) == ("image_ref", kind)


def test_an_uploaded_image_ref_passes() -> None:
    assert _hit("ask_image", {"image_ref": "expert_work://image/t/u/a.png"}) is None


# --- 提示文案(设计 §4.5)--------------------------------------------------------------


@pytest.mark.parametrize(
    ("hit", "expected"),
    [
        (
            LiteralHit(MCP, "fields", "variable", "attachment_urls", remote=True),
            "[blocked] 参数 fields 里的 $attachment_urls 不会被换成值，对方服务收到的就是这串字。"
            "请写出要传的实际内容；如果 attachment_urls 由平台填进绑定它的工具参数，就不用写。",
        ),
        (
            LiteralHit(MCP, "a", "env", "EXPERT_WORK_INPUTS_DIR", remote=True),
            "[blocked] 参数 a 里的 $EXPERT_WORK_INPUTS_DIR 只在 exec_python / bash 里有值，"
            "对方服务收到的就是这串字。",
        ),
        (
            LiteralHit(MCP, "attachments", "local_path", "", remote=True),
            "[blocked] 参数 attachments 是工作区里的本地路径，对方服务读不到沙箱里的文件。"
            "要传文件地址，请用绑定（由平台填）或原始链接。",
        ),
        (
            LiteralHit("read_document", "path", "variable", "report", remote=False),
            "[blocked] 参数 path 里的 $report 不会被换成值。"
            "要用 report 的文件，写 inputs/current/ 下的路径（见「本轮输入」段）。",
        ),
        (
            LiteralHit("read_file", "path", "env", "EXPERT_WORK_INPUTS", remote=False),
            "[blocked] 参数 path 里的 $EXPERT_WORK_INPUTS 只在 exec_python / bash 里有值。"
            "要用本轮输入文件，写 inputs/current/ 下的路径。",
        ),
        (
            LiteralHit("read_file", "path", "env", "EXPERT_WORK_SKILLS_DIR", remote=False),
            "[blocked] 参数 path 里的 $EXPERT_WORK_SKILLS_DIR 只在 exec_python / bash 里有值。"
            "读技能文件用 skill_view。",
        ),
        (
            LiteralHit("read_file", "path", "env", "EXPERT_WORK_OTHER", remote=False),
            "[blocked] 参数 path 里的 $EXPERT_WORK_OTHER 只在 exec_python / bash 里有值。",
        ),
    ],
)
def test_messages_are_verbatim(hit: LiteralHit, expected: str) -> None:
    assert literal_guard_message(hit) == expected


def test_remote_messages_never_point_at_a_local_path() -> None:
    """G8 —— 对方读不到沙箱文件:给 MCP / http 的提示里不出现 ``inputs/current``。"""
    for kind, name in (("variable", "org_logo"), ("env", "EXPERT_WORK_INPUTS_DIR")):
        hit = LiteralHit(MCP, "a", kind, name, remote=True)  # type: ignore[arg-type]
        assert "inputs/current" not in literal_guard_message(hit)


@pytest.mark.parametrize(
    "value",
    [
        "见 inputs/current/a.png",
        "file:///workspace/inputs/current/a.png",
        "![](inputs/current/a.png)",
        '{"img": "inputs/current/a.png"}',
        "inputs/current/a.png 和 inputs/current/b.png",
    ],
)
def test_a_path_inside_a_sentence_is_not_a_local_path(value: str) -> None:
    """规则 3 只认整段(设计 §六 列出的已知漏拦写法,接受),本轮有输入时也一样。"""
    assert _hit(MCP, {"text": value}, inputs_run=RUN) is None
