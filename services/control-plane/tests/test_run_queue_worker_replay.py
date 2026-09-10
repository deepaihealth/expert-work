"""P-1 —— queue worker 对 ``enqueued_input.replay_messages`` 的重放分支。

``:regenerate`` 的 queue 模式:``spawn_run`` 在锁内把被取代轮的 [System, Human]
原件序列化进 ``enqueued_input``,worker 认领后必须走 ``replay_graph_input``
(而不是拿一个 ``input=None`` 的空输入去跑)。第二条用例钉死这条链路的存亡
判据:那两条原件经 JSONB 列往返之后逐项相等。
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import uuid4

import pytest
from langchain_core.messages import HumanMessage, SystemMessage, message_to_dict

from control_plane import run_queue_worker as worker_mod
from expert_work.common.message_stamp import STAMP_RUN_ID
from expert_work.runtime.runs import InMemoryRunStore, RunManager
from tests.test_run_queue_worker import _FakeRuntime, _worker


async def _enqueue_replay(mgr: RunManager, *, run_id: Any) -> Any:
    tenant, thread = uuid4(), uuid4()
    await mgr.enqueue(
        run_id=run_id,
        thread_id=thread,
        tenant_id=tenant,
        enqueued_input={
            "replay_messages": [
                message_to_dict(SystemMessage(content="sys")),
                message_to_dict(HumanMessage(content="U-old")),
            ]
        },
    )
    return tenant


@pytest.mark.asyncio
async def test_worker_uses_replay_graph_input_when_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_run_agent(**kwargs: Any) -> None:
        captured["graph_input"] = kwargs["graph_input"]

    monkeypatch.setattr(worker_mod, "run_agent", fake_run_agent)
    store = InMemoryRunStore()
    runtime = _FakeRuntime(store)
    run_id = uuid4()
    await _enqueue_replay(runtime.run_manager, run_id=run_id)

    assert await _worker(store, runtime).run_once() == 1
    await asyncio.sleep(0)  # let the spawned task body run

    msgs = captured["graph_input"]["messages"]
    assert [type(m).__name__ for m in msgs] == ["SystemMessage", "HumanMessage"]
    assert msgs[1].content == "U-old"
    assert msgs[1].additional_kwargs[STAMP_RUN_ID] == str(run_id)
    assert "turn_documents" not in captured["graph_input"]


def test_replay_messages_survive_the_jsonb_round_trip() -> None:
    """``:regenerate`` queue 分支的存亡判据:旧轮 [System, Human] 原件经
    ``message_to_dict`` → JSON(JSONB 列)→ ``messages_from_dict`` 之后,``id`` /
    ``additional_kwargs``(run 戳 + 被取代标记)/ 多段 ``content``(text + image 块,
    ``build_run_graph_input`` 在 ``supports_vision=True`` 下的真实产出)逐项相等。
    只有 langchain 文档背书的往返,这里用真实形状钉死。
    """
    import json
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from langchain_core.messages import messages_from_dict

    from control_plane.api.runs import build_run_graph_input
    from expert_work.common.conversation_channel import SUPERSEDED_AT, SUPERSEDED_BY
    from expert_work.common.supersede import mark_superseded

    built = SimpleNamespace(
        supports_vision=True,
        spotlight_nonce=None,
        max_steps=5,
        max_no_progress=0,
        system_prompt="You are the replay probe.",
        prompt_jinja=False,
    )
    tenant_id, thread_id, old_run, new_run = uuid4(), uuid4(), uuid4(), uuid4()
    image_ref = f"expert_work://image/{tenant_id}/{thread_id}/{uuid4()}.png"
    graph_input = build_run_graph_input(
        built,
        input_text="看一下这张图",
        image_refs=[image_ref],
        untrusted_content=["<ticket>外部文本</ticket>"],
        run_id=old_run,
        document_names=["report.pdf"],
    )
    system, human = graph_input["messages"]
    # 真实多段形态,不是自己编的。
    assert isinstance(human.content, list) and len(human.content) >= 2
    # reducer 补 id 之前就带 id 的形态也要过。
    system = system.model_copy(update={"id": "sys-id-1"})
    human = mark_superseded(
        human.model_copy(update={"id": "human-id-1"}),
        new_run_id=str(new_run),
        now=datetime(2026, 9, 10, tzinfo=UTC),
    )

    # 走一遍 JSON,与 JSONB 列同形。
    wire = json.loads(json.dumps([message_to_dict(m) for m in (system, human)]))
    back_system, back_human = messages_from_dict(wire)

    assert (back_system.id, back_human.id) == ("sys-id-1", "human-id-1")
    assert back_system.content == system.content
    # 每个 block 的 type / text / image 引用逐项相等。
    assert back_human.content == human.content
    # STAMP_RUN_ID / created_at / SUPERSEDED_BY / SUPERSEDED_AT 全在。
    assert back_human.additional_kwargs == human.additional_kwargs
    assert back_human.additional_kwargs[STAMP_RUN_ID] == str(old_run)
    assert back_human.additional_kwargs[SUPERSEDED_BY] == str(new_run)
    assert SUPERSEDED_AT in back_human.additional_kwargs
    assert type(back_system).__name__ == "SystemMessage"
    assert type(back_human).__name__ == "HumanMessage"
