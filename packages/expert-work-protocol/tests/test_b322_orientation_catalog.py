"""B-322 PR ① —— 模型目录里「核实过能判图片方向」的标记与只增不减的撤销清单(设计 §4.1.9)。"""

from __future__ import annotations

import pytest

from expert_work.protocol import MODEL_CATALOG, ModelEntry, model_catalog
from expert_work.protocol.model_catalog import (
    ORIENTATION_REVOKED_MODELS,
    orientation_judge_allowed,
    orientation_model_label,
    orientation_revoked,
)
from expert_work.protocol.provider_catalog import Provider

#: 测试里另记一份撤销清单。常量**只增不减**:往常量里加名字时,这里也加;常量里少了这里的
#: 任何一个名字,下面那条测试就红(删条目、改名都不能「撤销撤销」)。
EVER_REVOKED: frozenset[str] = frozenset()


def test_revoked_list_only_grows() -> None:
    assert EVER_REVOKED <= ORIENTATION_REVOKED_MODELS


def test_orientation_verified_defaults_false() -> None:
    assert ModelEntry(name="x").orientation_verified is False


def test_only_qwen4_max_is_verified() -> None:
    """qwen4-max 过了 §6.2 E 组(2026-11-20:136 张,S3 0/0,W 0,H 1/430);别的没体检过,不设核实。"""
    verified = [
        f"{provider}/{entry.name}"
        for provider, entries in MODEL_CATALOG.items()
        for entry in entries
        if entry.orientation_verified
    ]
    assert verified == ["qwen/qwen4-max"]


def _mark_verified(monkeypatch: pytest.MonkeyPatch, provider: Provider, name: str) -> None:
    entries = tuple(
        e.model_copy(
            update={
                "orientation_verified": True,
                "max_image_pixels": e.max_image_pixels or 1_000_000,
            }
        )
        if e.name == name
        else e
        for e in MODEL_CATALOG[provider]
    )
    monkeypatch.setitem(MODEL_CATALOG, provider, entries)


def test_label_is_provider_slash_name() -> None:
    assert orientation_model_label("qwen", "qwen4-max") == "qwen/qwen4-max"


def test_judge_allowed_needs_the_verified_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    assert orientation_judge_allowed("qwen", "qwen4-max") is True
    assert orientation_judge_allowed("qwen", "qwen3.8-max") is False
    _mark_verified(monkeypatch, "qwen", "qwen3.8-max")
    assert orientation_judge_allowed("qwen", "qwen3.8-max") is True


def test_off_catalog_model_is_never_allowed() -> None:
    assert orientation_judge_allowed("qwen", "no-such-model") is False


def test_revoked_model_is_not_allowed_even_when_verified(monkeypatch: pytest.MonkeyPatch) -> None:
    _mark_verified(monkeypatch, "qwen", "qwen4-max")
    monkeypatch.setattr(model_catalog, "ORIENTATION_REVOKED_MODELS", frozenset({"qwen/qwen4-max"}))
    assert orientation_revoked("qwen/qwen4-max") is True
    assert orientation_judge_allowed("qwen", "qwen4-max") is False


def test_revocation_does_not_depend_on_the_catalog_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    """删掉 / 改名目录条目不会「撤销撤销」:撤销只看清单本身。"""
    monkeypatch.setattr(model_catalog, "ORIENTATION_REVOKED_MODELS", frozenset({"gone/old-name"}))
    assert orientation_revoked("gone/old-name") is True
    assert orientation_revoked("qwen/qwen4-max") is False


def test_verified_needs_a_known_per_image_budget() -> None:
    """单图预算是 ``orientation_verified`` 的前提:不知道就不能核实,功能也就不会对它运行。"""
    with pytest.raises(ValueError, match="max_image_pixels"):
        ModelEntry(name="x", orientation_verified=True)
    assert ModelEntry(
        name="x", orientation_verified=True, max_image_pixels=1_000
    ).orientation_verified


def test_judge_not_allowed_without_a_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    entries = tuple(
        e.model_copy(update={"orientation_verified": True, "max_image_pixels": None})
        if e.name == "qwen4-max"
        else e
        for e in MODEL_CATALOG["qwen"]
    )
    monkeypatch.setitem(MODEL_CATALOG, "qwen", entries)
    assert orientation_judge_allowed("qwen", "qwen4-max") is False


def test_qwen4_max_budget_is_the_measured_one() -> None:
    from expert_work.protocol import catalog_entry

    entry = catalog_entry("qwen", "qwen4-max")
    assert entry is not None
    assert entry.max_image_pixels == 12_300 * 1_024
