"""B-301 PR 2 —— 体检报价:单价核对、调用数 / token / 金额、止损线与硬上限、``quote_id``、CLI 拒跑。
全部离线。"""

from __future__ import annotations

import math
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import b301_health as cli
import b301_health_run as runner
import b301_plan as health

from orchestrator.multimodal import (
    DispatchingImageResolver,
    InMemoryImageResolver,
    NasWorkspaceImageResolver,
)

DOUBAO_OLD, DOUBAO_NEW = health.DEFAULT_MODELS


def test_prices_come_from_the_rate_card_migration_and_the_tier_rule() -> None:
    new = health.price_of(DOUBAO_NEW, accept_documented=False)
    old = health.price_of(DOUBAO_OLD, accept_documented=False)

    assert (new.input, new.output) == (6.0, 30.0)
    assert "0187" in new.source
    assert (old.input, old.output) == (6.0, 30.0)
    assert old.source.startswith("tier price")


def test_a_model_without_a_validated_price_is_refused() -> None:
    with pytest.raises(health.PlanError, match="no validated price"):
        health.price_of("doubao-seed-9-pro-999999", accept_documented=False)
    with pytest.raises(health.PlanError, match="accept-documented-prices"):
        health.price_of("glm-5.3-flashx", accept_documented=False)
    rows = {"qwen3.8-max-0902": health.Price(12.0, 36.0, 1.5, "x")}  # 260915 那一行没了
    with pytest.raises(health.PlanError, match="tier price source"):
        health.price_of(DOUBAO_OLD, accept_documented=False, rows=rows)


def test_quote_counts_tokens_and_money_for_the_two_doubao_models() -> None:
    q = health.quote(
        models=health.DEFAULT_MODELS, probe_models=(), stop_line=None, accept_documented=False
    )

    for model in health.DEFAULT_MODELS:
        entry = q["models"][model]
        # 问方向:136 张各 1 次 + 转过的 99 张各 3 次;读:开着 136 + 对照 99。
        assert entry["calls"] == {
            "orientation": 433,
            "orientation_worst": 544,
            "read": 235,
            "total": 668,
        }
        orient_in, read_in = 1361 + 150, 1361 + 150
        assert entry["tokens"] == {
            "input": 433 * orient_in + 235 * read_in,
            "output": 433 * 5 + 235 * 60,
        }
        expected = (entry["tokens"]["input"] * 6 + entry["tokens"]["output"] * 30) / 1e6
        assert entry["expected_yuan"] == round(expected, 2)
        assert entry["per_call_cap_yuan"] == round((8000 * 6 + 4000 * 30) / 1e6, 4)
    assert q["models"][DOUBAO_OLD]["image_tokens"]["source"].startswith("not measured")
    total = q["expected_total_yuan"]
    assert q["stop_line_yuan"] == math.ceil(max(q["worst_total_yuan"], total * 1.5))
    # 只有一家(豆包):在飞的最多 2 个 ask_image x 3 个并行调用,各按单次上限。
    assert q["hard_cap_yuan"] == round(q["stop_line_yuan"] + 6 * (8000 * 6 + 4000 * 30) / 1e6, 2)
    assert q["items"] == 2 * (136 + 99)


def test_quote_id_changes_with_the_stop_line_the_price_and_the_models() -> None:
    def qid(**kw: Any) -> str:
        args = {
            "models": health.DEFAULT_MODELS,
            "probe_models": (),
            "stop_line": 20.0,
            "accept_documented": False,
        }
        return str(health.quote(**{**args, **kw})["quote_id"])

    base = qid()
    assert qid() == base
    assert qid(stop_line=21.0) != base
    assert qid(models=(DOUBAO_NEW,)) != base
    cheaper = {**health.rate_card_rows(), DOUBAO_NEW: health.Price(5.0, 30.0, 1.2, "x")}
    assert qid(rows=cheaper) != base


def test_quote_refuses_a_stop_line_below_the_expected_spend() -> None:
    with pytest.raises(health.PlanError, match="below the expected"):
        health.quote(
            models=health.DEFAULT_MODELS, probe_models=(), stop_line=1.0, accept_documented=False
        )


def test_probe_needs_documented_prices_accepted_and_asks_70_images_once() -> None:
    with pytest.raises(health.PlanError):
        health.quote(
            models=(), probe_models=health.PROBE_MODELS, stop_line=None, accept_documented=False
        )
    q = health.quote(
        models=(), probe_models=health.PROBE_MODELS, stop_line=None, accept_documented=True
    )
    for model in health.PROBE_MODELS:
        assert q["models"][model]["calls"]["orientation"] == 70
        assert q["models"][model]["calls"]["read"] == 0


def test_run_refuses_a_quote_id_that_does_not_match(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    code = cli.main(["run", "--quote-id", "000000000000", "--results", str(tmp_path / "r.jsonl")])

    assert code == 2
    assert "refused" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    ("model", "provider"),
    [
        (DOUBAO_OLD, "doubao"),
        (DOUBAO_NEW, "doubao"),
        (health.QWEN_MODEL, "qwen"),
        ("glm-5.3-flashx", "glm"),
    ],
)
async def test_the_real_routers_cap_output_and_turn_thinking_off_without_calling(
    model: str, provider: str, tmp_path: Path
) -> None:
    """硬上限的前提:每次调用的输出被 ``max_tokens`` 截住;快看关思考(与生产快看同一条)。
    只建路由,不发请求。"""
    resolver = DispatchingImageResolver(
        uploads=InMemoryImageResolver(images={}), workspace=NasWorkspaceImageResolver(root=tmp_path)
    )
    callers = await runner.real_callers_factory({provider: "dummy-key"})(model, provider, resolver)

    assert callers.quick is not None
    handle = callers.quick.providers[0]  # type: ignore[attr-defined]
    assert health.MAX_OUTPUT_TOKENS in handle.provider.inner.output_cap_payload.values()
    assert handle.provider.limiter.max_rate == runner.EVAL_RATE_LIMIT_RPM
    assert handle.provider.inner.thinking_payload in (
        {"thinking": {"type": "disabled"}},
        {"enable_thinking": False},
        {"thinking": {"type": "enabled"}, "reasoning_effort": "low"},
    )
