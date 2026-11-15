from expert_work.protocol import MemoryItem
from expert_work.protocol.memory_item import effective_origin


def test_memory_item_access_count_defaults_zero() -> None:
    from uuid import uuid4

    from expert_work.protocol import MemoryItem

    item = MemoryItem(
        id=uuid4(),
        tenant_id=uuid4(),
        user_id=uuid4(),
        kind="fact",
        content="x",
        embedding=(0.1,),
    )
    assert item.access_count == 0


def test_memory_item_bitemporal_fields_default_none() -> None:
    from uuid import uuid4

    from expert_work.protocol import MemoryItem

    item = MemoryItem(
        id=uuid4(),
        tenant_id=uuid4(),
        user_id=uuid4(),
        kind="fact",
        content="user lives in Shanghai",
        embedding=(0.1, 0.2, 0.3),
    )
    assert item.source_run_id is None
    assert item.valid_at is None
    assert item.expired_at is None
    assert item.invalid_at is None
    assert item.supersedes is None
    assert item.superseded_by is None
    assert item.expected_valid_days is None


def test_memory_item_bitemporal_fields_roundtrip() -> None:
    from datetime import UTC, datetime
    from uuid import uuid4

    from expert_work.protocol import MemoryItem

    old_id = uuid4()
    run_id = uuid4()
    now = datetime(2026, 7, 21, tzinfo=UTC)
    item = MemoryItem(
        id=uuid4(),
        tenant_id=uuid4(),
        user_id=uuid4(),
        kind="fact",
        content="user lives in Beijing",
        embedding=(0.1, 0.2, 0.3),
        source_run_id=str(run_id),
        valid_at=now,
        supersedes=old_id,
        expected_valid_days=90,
    )
    assert item.source_run_id == str(run_id)
    assert item.valid_at == now
    assert item.supersedes == old_id
    assert item.expected_valid_days == 90


# ─── B-300 PR 2 —— 来源字段与「向量模型名」 ────────────────────────────────


def _origin_item(**overrides: object) -> MemoryItem:
    from uuid import uuid4

    fields: dict[str, object] = {
        "id": uuid4(),
        "tenant_id": uuid4(),
        "user_id": uuid4(),
        "kind": "fact",
        "content": "x",
        "embedding": (0.1,),
    }
    fields.update(overrides)
    return MemoryItem.model_validate(fields)


def test_origin_and_embedding_model_default_none() -> None:
    item = _origin_item()
    assert item.origin is None
    assert item.embedding_model is None


def test_known_origins_round_trip() -> None:
    for value in ("user_stated", "assistant_derived"):
        assert _origin_item(origin=value).origin == value


def test_unknown_origin_reads_as_none() -> None:
    """库里或检查点里出现不认识的值(以后加的值、手改的行)按「未记录」读,不报错。"""
    assert _origin_item(origin="something_new").origin is None
    assert _origin_item(origin=42).origin is None


def test_newer_payload_with_extra_fields_still_reads() -> None:
    """新版本写进检查点的记忆对象多了字段,旧定义照样能读回(模型不禁止多余字段)。"""
    item = _origin_item(origin="user_stated", some_future_field="v")
    assert item.origin == "user_stated"


def test_effective_origin_five_cases() -> None:
    # 置信度 1.0 只有更正接口会写:不论来源列是什么,都是用户更正。
    assert effective_origin(_origin_item(confidence=1.0)) == "user_corrected"
    assert (
        effective_origin(_origin_item(confidence=1.0, origin="assistant_derived"))
        == "user_corrected"
    )
    assert effective_origin(_origin_item(origin="user_stated")) == "user_stated"
    assert effective_origin(_origin_item(origin="user_confirmed")) == "user_confirmed"
    assert effective_origin(_origin_item(origin="assistant_derived")) == "assistant_derived"
    assert effective_origin(_origin_item()) == "unrecorded"


# ─── B-300 重做 PR M —— 第三个来源值「用户确认」与证据指针、事件日期、确认日期 ─────────


def test_user_confirmed_is_a_known_origin() -> None:
    """重做设计 §6.2:来源加第三个值 ``user_confirmed``,读写都原样保留。"""
    assert _origin_item(origin="user_confirmed").origin == "user_confirmed"


def test_effective_origin_user_confirmed_sits_below_correction() -> None:
    """有效来源的顺序:置信度 1.0 → 用户更正;否则 用户说的 → 用户确认 → 来自助手 → 未记录。"""
    assert effective_origin(_origin_item(origin="user_confirmed")) == "user_confirmed"
    assert (
        effective_origin(_origin_item(origin="user_confirmed", confidence=1.0)) == "user_corrected"
    )


def test_evidence_event_and_confirmed_fields_default_none() -> None:
    """老行、老检查点里的记忆对象没有这四个字段:读出来都是空。"""
    item = _origin_item()
    assert item.evidence_message_id is None
    assert item.evidence_message_index is None
    assert item.occurred_on is None
    assert item.confirmed_at is None


def test_evidence_event_and_confirmed_fields_round_trip() -> None:
    """证据指针(消息 id + 序号)、事件日期(只到天)、确认日期;经 JSON 往返不变
    (记忆对象会进检查点)。"""
    from datetime import UTC, date, datetime

    confirmed = datetime(2026, 11, 15, 8, 30, tzinfo=UTC)
    item = _origin_item(
        evidence_message_id="msg-7",
        evidence_message_index=7,
        occurred_on="2026-11-14",
        confirmed_at=confirmed,
    )
    assert item.evidence_message_id == "msg-7"
    assert item.evidence_message_index == 7
    assert item.occurred_on == date(2026, 11, 14)
    assert item.confirmed_at == confirmed
    again = MemoryItem.model_validate_json(item.model_dump_json())
    assert (again.evidence_message_id, again.evidence_message_index) == ("msg-7", 7)
    assert (again.occurred_on, again.confirmed_at) == (date(2026, 11, 14), confirmed)
