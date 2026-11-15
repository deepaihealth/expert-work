"""B-300 ① —— 用户在对话里的输入只有一个构造点,它给消息记下「哪一段是用户输入的文字」。

长期记忆的第一次提炼只读这一段(白名单);附件、外部内容、定时任务的种子、子智能体的任务说明
都不算用户的话(设计 §4.1、§8.4 第一行)。
"""

from __future__ import annotations

import ast
from pathlib import Path

from control_plane.api.runs import _build_human_message
from expert_work.common.user_input import USER_INPUT_KEY, read_user_input

_REPO = Path(__file__).resolve().parents[3]


def test_text_only_input_is_marked_whole() -> None:
    msg = _build_human_message(input_text="我对花生过敏", image_refs=[], supports_vision=False)
    got = read_user_input(msg)
    assert got is not None
    assert got.text == "我对花生过敏"
    assert (got.images, got.files, got.external_blocks) == (0, 0, 0)


def test_attachments_and_external_content_are_counted_not_marked_as_text() -> None:
    msg = _build_human_message(
        input_text="帮我看看",
        image_refs=["expert_work://img/1", "expert_work://img/2"],
        supports_vision=False,
        untrusted_content=["EXTERNAL_SENTINEL_ONE", "EXTERNAL_SENTINEL_TWO"],
        document_names=["uploads/a.pdf"],
    )
    got = read_user_input(msg)
    assert got is not None
    assert got.text == "帮我看看"
    assert (got.images, got.files, got.external_blocks) == (2, 1, 2)
    assert "EXTERNAL_SENTINEL" not in got.text


def test_vision_content_blocks_mark_the_first_text_block() -> None:
    msg = _build_human_message(
        input_text="这张图里是什么",
        image_refs=["expert_work://img/1"],
        supports_vision=True,
        untrusted_content=["EXTERNAL_SENTINEL"],
        spotlight_nonce="n0nce",
        document_names=["uploads/b.docx"],
    )
    assert isinstance(msg.content, list)
    got = read_user_input(msg)
    assert got is not None
    assert got.text == "这张图里是什么"
    assert (got.images, got.files, got.external_blocks) == (1, 1, 1)


def test_attachment_only_input_has_empty_text() -> None:
    msg = _build_human_message(
        input_text=None, image_refs=[], supports_vision=False, document_names=["uploads/a.pdf"]
    )
    got = read_user_input(msg)
    assert got is not None
    assert got.text == ""
    assert got.files == 1


def _source_files() -> list[Path]:
    roots = [*(_REPO / "packages").glob("*/src"), *(_REPO / "services").glob("*/src")]
    return [path for root in roots for path in root.rglob("*.py")]


def test_only_build_human_message_writes_the_mark() -> None:
    """遍历仓库全部产品源码:写这个标记的只有 ``_build_human_message``。

    别处(定时任务种子、子智能体任务说明、评测引擎……)哪天也写了,第一次提炼就会把它们当成用户的话。
    """
    writers: list[str] = []
    for path in _source_files():
        text = path.read_text(encoding="utf-8")
        rel = path.relative_to(_REPO).as_posix()
        if rel.endswith("expert_work/common/user_input.py"):
            continue  # 定义处
        if "user_input_kwargs" in text or USER_INPUT_KEY in text or "USER_INPUT_KEY" in text:
            writers.append(rel)
    assert writers == ["services/control-plane/src/control_plane/api/runs.py"]

    runs = _REPO / "services/control-plane/src/control_plane/api/runs.py"
    tree = ast.parse(runs.read_text(encoding="utf-8"))
    callers = {
        func.name
        for func in ast.walk(tree)
        if isinstance(func, ast.FunctionDef | ast.AsyncFunctionDef)
        for node in ast.walk(func)
        if isinstance(node, ast.Name) and node.id in ("user_input_kwargs", "USER_INPUT_KEY")
    }
    assert callers == {"_build_human_message"}


def test_construction_point_stamps_the_send_time() -> None:
    from datetime import UTC, datetime, timedelta

    before = datetime.now(UTC)
    msg = _build_human_message(input_text="hello", image_refs=[], supports_vision=False)
    got = read_user_input(msg)
    assert got is not None and got.sent_at is not None
    sent = datetime.fromisoformat(got.sent_at)
    assert sent.tzinfo is not None
    assert before - timedelta(seconds=1) <= sent <= datetime.now(UTC) + timedelta(seconds=1)
