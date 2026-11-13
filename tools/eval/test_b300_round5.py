"""B-300 评测脚本第五轮审查的修复(结果有效性):离线。"""

from __future__ import annotations

import json
import math
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import b300_eval as cli
import b300_phases as phases
import b300_report as rep
import b300_scenarios as sc
import b300_spend as spend
import pytest

TENANT = UUID("11111111-1111-4111-8111-111111111111")


def _quote(tmp_path: Path, *extra: str) -> dict[str, Any]:
    out: list[str] = []
    code = cli.main(
        ["quote", "--accept-documented-prices", "--state-dir", str(tmp_path / "state"), *extra],
        emit=out.append,
    )
    assert code == 0, out
    return json.loads(out[0])


# --------------------------------------------------------------------------- MEDIUM 1


def _old_out(
    tmp_path: Path, q: dict[str, Any], *, release: str = "rel-1", commit: str | None = None
) -> Path:
    """旧目录:``c`` 段做完三分之一,带 meta(发布版本 + 脚本提交)。"""
    old = tmp_path / "old"
    units = phases.build_units(phases.PHASES["c"], q)
    phases.save_units(old, "c", units, q)
    scenarios = phases.all_scenarios()
    done = units[: len(units) // 3]
    (old / "c" / "records.jsonl").write_text(
        "".join(
            json.dumps(
                {
                    **u,
                    "key": phases.unit_key(u),
                    "convs": [{"run_id": "r", "final_text": "a"}]
                    * len(scenarios[u["scenario_id"]].conversations),
                }
            )
            + "\n"
            for u in done
        )
    )
    meta = {
        "phase": "c",
        "release_tag": release,
        "script_commit": commit or phases.git_commit(),
        "recorded_at": "2026-11-13T08:00:00+00:00",
    }
    (old / "c" / "meta.json").write_text(json.dumps(meta))
    return old


def _resume(tmp_path: Path, old: Path, *, release: str = "rel-1") -> tuple[int, list[str]]:
    progress = tmp_path / "progress.json"
    assert (
        cli.main(["progress", "--out", str(old), "--write", str(progress)], emit=lambda _l: None)
        == 0
    )
    q = _quote(tmp_path, "--phases", "c", "--progress", str(progress))
    lines: list[str] = []
    code = cli.main(
        [
            "prepare",
            "--phase",
            "c",
            "--out",
            str(tmp_path / "new"),
            "--tenant-id",
            str(TENANT),
            "--approved-quote",
            q["quote_id"],
            "--resume-from",
            str(old),
            "--release-tag",
            release,
            "--phases",
            "c",
            "--progress",
            str(progress),
            "--accept-documented-prices",
            "--state-dir",
            str(tmp_path / "state"),
        ],
        emit=lines.append,
    )
    return code, lines


def test_units_carry_model_ids_and_scenario_digest(tmp_path: Path) -> None:
    q = _quote(tmp_path)
    (u,) = [x for x in phases.build_units(phases.PHASES["c"], q) if x["vendor"] == "glm"][:1]
    assert u["models"] == {"main": "glm-5.3", "memory": "glm-5.3-flash"}
    scenario = phases.all_scenarios()[u["scenario_id"]]
    assert u["scenario_digest"] == sc.scenario_digest([scenario])


def test_resume_with_matching_versions_carries(tmp_path: Path) -> None:
    q = _quote(tmp_path, "--phases", "c")
    code, lines = _resume(tmp_path, _old_out(tmp_path, q))
    assert code == 0, lines
    meta = json.loads((tmp_path / "new" / "c" / "meta.json").read_text())
    assert meta["release_tag"] == "rel-1" and meta["script_commit"] == phases.git_commit()


def test_resume_refuses_another_release(tmp_path: Path) -> None:
    q = _quote(tmp_path, "--phases", "c")
    code, lines = _resume(tmp_path, _old_out(tmp_path, q, release="rel-1"), release="rel-2")
    assert code == 2 and "release" in lines[-1]


def test_resume_refuses_another_script_commit(tmp_path: Path) -> None:
    q = _quote(tmp_path, "--phases", "c")
    code, lines = _resume(tmp_path, _old_out(tmp_path, q, commit="0" * 40))
    assert code == 2 and "commit" in lines[-1]


def test_resume_refuses_records_from_other_models(tmp_path: Path) -> None:
    q = _quote(tmp_path, "--phases", "c")
    old = _old_out(tmp_path, q)
    rows = [json.loads(x) for x in (old / "c" / "records.jsonl").read_text().splitlines()]
    rows[0]["models"] = {"main": "some-other-model", "memory": "glm-5.3-flash"}
    (old / "c" / "records.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    code, lines = _resume(tmp_path, old)
    assert code == 2 and "model" in lines[-1]


def test_resume_refuses_records_of_changed_scenarios(tmp_path: Path) -> None:
    q = _quote(tmp_path, "--phases", "c")
    old = _old_out(tmp_path, q)
    rows = [json.loads(x) for x in (old / "c" / "records.jsonl").read_text().splitlines()]
    rows[0]["scenario_digest"] = "deadbeef0000"
    (old / "c" / "records.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    code, lines = _resume(tmp_path, old)
    assert code == 2 and "scenario" in lines[-1]


def test_resume_refuses_records_without_a_fingerprint(tmp_path: Path) -> None:
    q = _quote(tmp_path, "--phases", "c")
    old = _old_out(tmp_path, q)
    rows = [json.loads(x) for x in (old / "c" / "records.jsonl").read_text().splitlines()]
    del rows[0]["models"]
    (old / "c" / "records.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    code, lines = _resume(tmp_path, old)
    assert code == 2 and "model" in lines[-1]


# the three earlier resume refusals (LOW)


def test_resume_refuses_an_already_prepared_out(tmp_path: Path) -> None:
    q = _quote(tmp_path, "--phases", "c")
    old = _old_out(tmp_path, q)
    assert _resume(tmp_path, old)[0] == 0
    code, lines = _resume(tmp_path, old)
    assert code == 2 and "already prepared" in lines[-1]


def test_resume_refuses_resuming_into_itself(tmp_path: Path) -> None:
    q = _quote(tmp_path, "--phases", "c")
    old = _old_out(tmp_path, q)
    progress = tmp_path / "progress.json"
    cli.main(["progress", "--out", str(old), "--write", str(progress)], emit=lambda _l: None)
    q2 = _quote(tmp_path, "--phases", "c", "--progress", str(progress))
    lines: list[str] = []
    code = cli.main(
        [
            "prepare",
            "--phase",
            "c4",
            "--out",
            str(old),
            "--tenant-id",
            str(TENANT),
            "--approved-quote",
            q2["quote_id"],
            "--resume-from",
            str(old),
            "--release-tag",
            "rel-1",
            "--phases",
            "c",
            "--progress",
            str(progress),
            "--accept-documented-prices",
            "--state-dir",
            str(tmp_path / "state"),
        ],
        emit=lines.append,
    )
    assert code == 2 and "old out dir" in lines[-1]


def test_resume_needs_a_quote_made_with_progress(tmp_path: Path) -> None:
    q = _quote(tmp_path, "--phases", "c")
    old = _old_out(tmp_path, q)
    lines: list[str] = []
    code = cli.main(
        [
            "prepare",
            "--phase",
            "c",
            "--out",
            str(tmp_path / "new"),
            "--tenant-id",
            str(TENANT),
            "--approved-quote",
            q["quote_id"],
            "--resume-from",
            str(old),
            "--release-tag",
            "rel-1",
            "--phases",
            "c",
            "--accept-documented-prices",
            "--state-dir",
            str(tmp_path / "state"),
        ],
        emit=lines.append,
    )
    assert code == 2 and "--progress" in lines[-1]


# --------------------------------------------------------------------------- MEDIUM 2


class _KeyEmbedder:
    """按「含不含 X / Y 的关键串」给方向:像 X 的几条方向一样(余弦 1),Y 另一个方向。只验
    「几条有效、谁被排除」的逻辑,不验真实向量的相似度。"""

    def __init__(self, x_key: str, y_key: str) -> None:
        self.keys = ("".join(x_key.split()).lower(), "".join(y_key.split()).lower())

    def vec(self, text: str) -> tuple[float, ...]:
        norm = "".join(text.split()).lower()
        has_x, has_y = (k in norm for k in self.keys)
        return (1.0, 0.0) if has_x and not has_y else (0.0, 1.0)


class _FakeStore:
    """整理取候选只用到的两个方法;``exclude_superseded`` 模拟 PR A 的 D1 前 / 后。"""

    def __init__(self, items: list[Any], *, exclude_superseded: bool) -> None:
        self.items, self.exclude = items, exclude_superseded

    def _live(self) -> list[Any]:
        return [i for i in self.items if not (self.exclude and i.invalid_at is not None)]

    async def list_transient(self, **_: Any) -> list[Any]:
        return self._live()

    async def list_rejected_clusters(self, **_: Any) -> list[Any]:
        return []  # PR A 的「被拒的簇」标记:这里没有

    async def vector_neighbors(self, *, embedding: Any, cosine_max: float, **_: Any) -> list[Any]:
        def dist(v: Any) -> float:
            dot = sum(a * b for a, b in zip(v, embedding, strict=True))
            norm = math.sqrt(sum(a * a for a in v)) * math.sqrt(sum(b * b for b in embedding))
            return 1 - dot / norm

        return [i for i in self._live() if dist(i.embedding) <= cosine_max]


def _items(scenario: sc.Scenario) -> dict[str, Any]:
    from expert_work.protocol import MemoryItem

    emb = _KeyEmbedder(scenario.x_key, scenario.y_key)
    user = uuid4()
    now = datetime.now(UTC)
    out = {}
    for seed in scenario.seeds:
        out[seed.key] = MemoryItem(
            id=uuid4(),
            tenant_id=TENANT,
            user_id=user,
            kind="fact",
            content=seed.content,
            embedding=emb.vec(seed.content),
            created_at=now - timedelta(days=seed.days_ago),
            invalid_at=now if seed.superseded_by else None,
        )
    return out


async def _clusters(
    scenario: sc.Scenario, *, exclude_superseded: bool
) -> tuple[dict[str, Any], list[set[Any]]]:
    from types import SimpleNamespace

    from control_plane.memory_consolidator import MemoryConsolidator

    items = _items(scenario)
    consolidator = MemoryConsolidator(
        memory_store=_FakeStore(list(items.values()), exclude_superseded=exclude_superseded),  # type: ignore[arg-type]
        tenant_config_service=SimpleNamespace(),  # type: ignore[arg-type]
        audit_logger=SimpleNamespace(),  # type: ignore[arg-type]
        aux_model=SimpleNamespace(),  # type: ignore[arg-type]
        embedder=SimpleNamespace(),  # type: ignore[arg-type]
    )
    clusters = await consolidator._find_candidate_clusters(
        tenant_id=TENANT, user_id=uuid4(), min_cluster_size=3, similarity=0.85
    )
    return items, [{m.id for m in c} for c in clusters]


C5 = [s for s in sc.build_group("C") if s.cls == "C5"]


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", C5, ids=lambda s: s.scenario_id)
async def test_c5_seeds_cluster_before_and_after_d1(scenario: sc.Scenario) -> None:
    """改前(作废的 X 还进候选):X 在簇里;改后(D1 排除作废的):簇照样凑得成、X 不在里面。"""
    items, before = await _clusters(scenario, exclude_superseded=False)
    assert any(items["x"].id in c for c in before)
    items, after = await _clusters(scenario, exclude_superseded=True)
    assert after, "post-D1 the still-valid near-duplicates must still form a cluster"
    assert not any(items["x"].id in c for c in after)
    valid_x_like = [
        k for k, s in ((s.key, s) for s in scenario.seeds) if s.superseded_by is None and k != "y"
    ]
    assert len(valid_x_like) >= 3


@pytest.mark.asyncio
async def test_old_two_near_seeds_would_not_cluster_after_d1() -> None:
    """对照(说明为什么加第三条):只有两条相近的,D1 之后凑不满 3 条,C5 ① 什么都量不到。"""
    scenario = C5[0]
    trimmed = replace(scenario, seeds=tuple(s for s in scenario.seeds if s.key != "near3"))
    _, after = await _clusters(trimmed, exclude_superseded=True)
    assert after == []


def test_post_change_no_cluster_is_not_measured_not_a_pass(tmp_path: Path) -> None:
    s = C5[0]
    seed_ids = {"x": "mx", "y": "my", "near1": "n1", "near2": "n2", "near3": "n3"}
    rows = [
        {
            "phase": "c5",
            "scenario_id": s.scenario_id,
            "cls": "C5",
            "domain": s.domain,
            "vendor": "glm",
            "repeat": 0,
            "attempt": 0,
            "user": "u5",
            "key": "k",
            "seed_ids": seed_ids,
            "convs": [{"run_id": "r", "final_text": "Y"}],
        }
    ]
    (tmp_path / "c5").mkdir()
    (tmp_path / "c5" / "records.jsonl").write_text(json.dumps(rows[0]) + "\n")
    (tmp_path / "c5" / "inpod-recalled.jsonl").write_text(
        json.dumps({"run_id": "r", "memory_ids": ["my"]}) + "\n"
    )
    (tmp_path / "c5" / "inpod-candidates.jsonl").write_text(
        json.dumps({"key": "u5", "clusters": []}) + "\n"
    )
    (tmp_path / "c5" / "inpod-state.jsonl").write_text(json.dumps({"key": "u5", "rows": []}) + "\n")
    body = cli.report(tmp_path, None)
    assert body["counts"]["c5"]["c5_not_measured"] == 1
    assert "C5.summary_violation" not in body["lines"]  # 不算进过线
    assert any("未测到" in d for d in body["disclosures"])


def test_region_includes_the_third_near_duplicate() -> None:
    seeds = {"x": "x", "near1": "a", "near2": "b", "near3": "c"}
    assert rep.c5_region_in_candidates(seeds, [["a", "b", "c"]])["region_clustered"] is True


# --------------------------------------------------------------------------- LOW


@pytest.mark.parametrize("value", ["true", "yes", "0", " 1", "2"])
def test_test_switch_accepts_only_exactly_one(
    monkeypatch: pytest.MonkeyPatch, value: str, tmp_path: Path
) -> None:
    monkeypatch.setenv(spend.TEST_STATE_DIR_ENV, value)
    with pytest.raises(spend.StateDirError):
        spend.check_state_dir(tmp_path)
    monkeypatch.setenv(spend.TEST_STATE_DIR_ENV, "1")
    assert spend.check_state_dir(tmp_path) == tmp_path
