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


def test_no_model_is_verified_yet() -> None:
    """PR ① 合并时没有任何模型过了 §6.2 E 组:设核实要先跑体检、过线、再在这里改。"""
    verified = [
        f"{provider}/{entry.name}"
        for provider, entries in MODEL_CATALOG.items()
        for entry in entries
        if entry.orientation_verified
    ]
    assert verified == []


def _mark_verified(monkeypatch: pytest.MonkeyPatch, provider: Provider, name: str) -> None:
    entries = tuple(
        e.model_copy(update={"orientation_verified": True}) if e.name == name else e
        for e in MODEL_CATALOG[provider]
    )
    monkeypatch.setitem(MODEL_CATALOG, provider, entries)


def test_label_is_provider_slash_name() -> None:
    assert orientation_model_label("qwen", "qwen4-max") == "qwen/qwen4-max"


def test_judge_allowed_needs_the_verified_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    assert orientation_judge_allowed("qwen", "qwen4-max") is False
    _mark_verified(monkeypatch, "qwen", "qwen4-max")
    assert orientation_judge_allowed("qwen", "qwen4-max") is True


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
