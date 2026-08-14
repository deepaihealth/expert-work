"""MessageTurn 的 P2 新增字段 —— 默认 None,不破坏既有构造。"""

from datetime import UTC, datetime
from uuid import uuid4

from expert_work.persistence import MessageTurn


def test_new_fields_default_to_none() -> None:
    turn = MessageTurn(seq=0, role="user", content="你好")
    assert turn.created_at is None
    assert turn.run_id is None


def test_new_fields_round_trip() -> None:
    now = datetime.now(UTC)
    rid = uuid4()
    turn = MessageTurn(
        seq=1,
        role="assistant",
        content="答案",
        channel="final",
        created_at=now,
        run_id=rid,
    )
    assert turn.created_at == now
    assert turn.run_id == rid
