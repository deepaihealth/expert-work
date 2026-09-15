"""B-60 —— ``sandbox_instance.layout`` 三份拷贝互相钉住:
``sandbox_instance_store.SANDBOX_LAYOUT_USER_ROOT`` 常量 ↔ ORM 列的
``server_default``(``sandbox_instance.py``)↔ 迁移 0155 的
``server_default``(``0155_sandbox_instance_layout.py``)。常量一旦改动而
两处 server_default 没有跟着改,存量热会话在 acquire 时会被误判为
``layout_mismatch``,整个热池被销毁重建。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import Text

from expert_work.persistence.models.sandbox_instance import SandboxInstanceRow
from expert_work.persistence.sandbox_instance_store import SANDBOX_LAYOUT_USER_ROOT

_MIGRATION_PATH = (
    Path(__file__).parent.parent / "migrations" / "versions" / "0155_sandbox_instance_layout.py"
)


def test_layout_column_is_text_not_null_default_user_root() -> None:
    col = SandboxInstanceRow.__table__.c["layout"]
    assert isinstance(col.type, Text)
    assert col.nullable is False
    assert col.server_default is not None
    # 与 SANDBOX_LAYOUT_USER_ROOT 常量同一个字面量;存量行(0155 之前建的热会话)
    # 全部落这个值,acquire 据此把它们销毁重建。
    assert col.server_default.arg.text == f"'{SANDBOX_LAYOUT_USER_ROOT}'"


def test_migration_0155_server_default_matches_sandbox_layout_user_root() -> None:
    spec = importlib.util.spec_from_file_location(
        "_migration_0155_sandbox_instance_layout", _MIGRATION_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    with patch.object(module.op, "add_column") as mock_add_column:
        module.upgrade()

    _, column = mock_add_column.call_args.args
    assert column.server_default.arg == SANDBOX_LAYOUT_USER_ROOT
