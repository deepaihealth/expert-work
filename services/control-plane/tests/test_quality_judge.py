"""Unit tests for :class:`QualityJudge` — Stream RT-5 (RT-ADR-23).

Drives ``score`` over a stubbed ``build_llm_router`` (patched on the
``orchestrator`` module the judge lazily imports) + a fake credentials
resolver, so no real provider / vault is touched.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage

from control_plane.quality_judge import QualityJudge


class _FakeResolver:
    def __init__(self, *, raises: bool = False) -> None:
        self._raises = raises

    async def resolve_provider(self, *, tenant_id: Any, provider: Any) -> str:
        if self._raises:
            msg = "no platform credential"
            raise RuntimeError(msg)
        return "secret://provider/anthropic"


def _patch_router(monkeypatch: pytest.MonkeyPatch, response: AIMessage) -> None:
    async def _fake_router(*, messages: Any, tools: Any, output_schema: Any) -> AIMessage:
        return response

    async def _fake_build(spec: Any, *, secret_store: Any, **kwargs: Any) -> Any:
        return _fake_router

    monkeypatch.setattr("orchestrator.build_llm_router", _fake_build, raising=False)


_JUDGE_PROVIDER = "anthropic"
_JUDGE_MODEL = "claude-haiku-4-5-20251001"


def _judge(resolver: _FakeResolver) -> QualityJudge:
    return QualityJudge(
        resolver=resolver,  # type: ignore[arg-type]
        secret_store=object(),  # type: ignore[arg-type]
    )


async def _score(judge: QualityJudge, *, prompt: str = "q", reply: str = "a"):
    return await judge.score(
        tenant_id=uuid4(),
        prompt=prompt,
        reply=reply,
        provider=_JUDGE_PROVIDER,
        model=_JUDGE_MODEL,
    )


@pytest.mark.asyncio
async def test_score_returns_structured_verdict_and_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = AIMessage(
        content="",
        additional_kwargs={
            "parsed": {
                "overall": 4,
                "addressed_request": 5,
                "coherence": 4,
                "safety": 5,
                "rationale": "Handled the refund cleanly.",
            }
        },
        usage_metadata={"input_tokens": 120, "output_tokens": 18, "total_tokens": 138},
    )
    _patch_router(monkeypatch, response)
    result = await _score(
        _judge(_FakeResolver()), prompt="I was charged twice", reply="Refund opened."
    )
    assert result is not None
    assert result.overall == 4
    assert result.dimensions == {"addressed_request": 5, "coherence": 4, "safety": 5}
    assert result.input_tokens == 120
    assert result.output_tokens == 18
    assert result.model == "claude-haiku-4-5-20251001"


@pytest.mark.asyncio
async def test_score_returns_none_when_verdict_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_router(monkeypatch, AIMessage(content="no structured output"))
    result = await _score(_judge(_FakeResolver()))
    assert result is None


@pytest.mark.asyncio
async def test_score_returns_none_on_out_of_range_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Router validation is mocked away; the judge's own model_validate guards.
    response = AIMessage(
        content="",
        additional_kwargs={
            "parsed": {
                "overall": 9,  # out of 1..5
                "addressed_request": 5,
                "coherence": 4,
                "safety": 5,
                "rationale": "x",
            }
        },
    )
    _patch_router(monkeypatch, response)
    result = await _score(_judge(_FakeResolver()))
    assert result is None


@pytest.mark.asyncio
async def test_score_never_raises_on_credential_failure() -> None:
    # No router patch needed — the resolver raises before the router is built.
    result = await _score(_judge(_FakeResolver(raises=True)))
    assert result is None
