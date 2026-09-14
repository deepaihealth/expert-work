"""B-60 —— ``sandbox_instance.layout``:热会话的沙箱内布局版本(迁移 0155)。"""

from __future__ import annotations

from sqlalchemy import Text

from expert_work.persistence.models.sandbox_instance import SandboxInstanceRow


def test_layout_column_is_text_not_null_default_user_root() -> None:
    col = SandboxInstanceRow.__table__.c["layout"]
    assert isinstance(col.type, Text)
    assert col.nullable is False
    assert col.server_default is not None
    # 与迁移 0155 的 server_default 同一个字面量;存量行(0155 之前建的热会话)
    # 全部落这个值,acquire 据此把它们销毁重建。
    assert col.server_default.arg.text == "'user-root'"
