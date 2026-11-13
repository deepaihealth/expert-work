"""B-300 评测脚本第六轮审查的 LOW:C5 的几种「没测到」各记各的原因、平台汇总行另判另列,以及
四个此前不咬的变异各补一条测试。离线。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import b300_eval as cli
import b300_phases as phases
import b300_report as rep
import b300_scenarios as sc

TENANT = "11111111-1111-4111-8111-111111111111"
C5 = next(s for s in sc.build_group("C") if s.cls == "C5")
SEEDS = {"x": "mx", "y": "my", "near1": "n1", "near2": "n2", "near3": "n3"}


def _c5_out(tmp_path: Path, *, candidates: dict[str, Any] | None, summaries: list[str]) -> Path:
    out = tmp_path / "out"
    (out / "c5").mkdir(parents=True)
    rec = {
        "phase": "c5",
        "scenario_id": C5.scenario_id,
        "cls": "C5",
        "domain": C5.domain,
        "vendor": "glm",
        "repeat": 0,
        "attempt": 0,
        "user": "u5",
        "key": "k",
        "seed_ids": SEEDS,
        "convs": [{"run_id": "r", "final_text": "Y"}],
    }
    (out / "c5" / "records.jsonl").write_text(json.dumps(rec) + "\n")
    (out / "c5" / "inpod-recalled.jsonl").write_text(
        json.dumps({"run_id": "r", "memory_ids": ["my"]}) + "\n"
    )
    if candidates is not None:
        (out / "c5" / "inpod-candidates.jsonl").write_text(
            json.dumps({"key": "u5", **candidates}) + "\n"
        )
    rows = [{"id": f"s{i}", "summary": True, "content": c} for i, c in enumerate(summaries)]
    (out / "c5" / "inpod-state.jsonl").write_text(json.dumps({"key": "u5", "rows": rows}) + "\n")
    return out


def _summary_items(out: Path) -> list[str]:
    items, _facts = cli.judge_items(out, phases.PHASES["c5"])
    return [i.key for i in items if i.check in {"summary", "platform_summary"}]


def test_platform_pre_consolidation_has_its_own_reason_and_separate_verdicts(
    tmp_path: Path,
) -> None:
    out = _c5_out(
        tmp_path, candidates={"clusters": [], "already_consolidated": True}, summaries=["It was X."]
    )
    assert _summary_items(out) == ["u5|platform_summary"]  # 平台的汇总行也送判官
    (out / "c5" / "verdicts.jsonl").write_text(
        json.dumps({"key": "u5|platform_summary", "verdict": True}) + "\n"
    )
    body = cli.report(out, None)
    counts = body["counts"]["c5"]
    assert counts["c5_consolidated_before_our_step"] == 1
    assert counts.get("c5_not_measured", 0) == 0  # 不是记成「没凑成簇」
    assert counts["c5_platform_summary_x_as_current"] == 1  # 另列
    assert "C5.summary_violation" not in body["lines"]  # 不进过线


def test_missing_candidate_record_is_its_own_reason(tmp_path: Path) -> None:
    out = _c5_out(tmp_path, candidates=None, summaries=["It changed from X to Y."])
    assert _summary_items(out) == []  # 不判
    counts = cli.report(out, None)["counts"]["c5"]
    assert counts["c5_candidates_not_recorded"] == 1 and counts.get("c5_not_measured", 0) == 0


def test_no_cluster_summaries_are_not_sent_to_the_judge(tmp_path: Path) -> None:
    out = _c5_out(tmp_path, candidates={"clusters": [["mx", "zz"]]}, summaries=["It was X."])
    assert _summary_items(out) == []
    assert cli.report(out, None)["counts"]["c5"]["c5_not_measured"] == 1


def test_cluster_check_counts_the_third_near_duplicate() -> None:
    assert rep.c5_region_in_candidates(SEEDS, [["n1", "n3"]])["region_clustered"] is True
    assert rep.c5_region_in_candidates(SEEDS, [["n1", "zz"]])["region_clustered"] is False


# ------------------------------------------------------------------ resume


def _quote(tmp_path: Path, *extra: str) -> dict[str, Any]:
    out: list[str] = []
    code = cli.main(
        ["quote", "--accept-documented-prices", "--state-dir", str(tmp_path / "state"), *extra],
        emit=out.append,
    )
    assert code == 0, out
    return json.loads(out[0])


def _old(tmp_path: Path, q: dict[str, Any], meta: dict[str, Any] | None) -> Path:
    old = tmp_path / "old"
    units = phases.build_units(phases.PHASES["c"], q)
    phases.save_units(old, "c", units, q)
    (old / "c" / "records.jsonl").write_text(
        json.dumps(
            {
                **units[0],
                "key": phases.unit_key(units[0]),
                "convs": [{"run_id": "r", "final_text": "a"}],
            }
        )
        + "\n"
    )
    if meta is not None:
        (old / "c" / "meta.json").write_text(json.dumps(meta))
    return old


def _prepare_resume(tmp_path: Path, old: Path, phase: str, *extra: str) -> tuple[int, list[str]]:
    progress = tmp_path / "progress.json"
    cli.main(["progress", "--out", str(old), "--write", str(progress)], emit=lambda _l: None)
    q = _quote(tmp_path, "--phases", "c", "--progress", str(progress))
    lines: list[str] = []
    code = cli.main(
        [
            "prepare",
            "--phase",
            phase,
            "--out",
            str(tmp_path / "new"),
            "--tenant-id",
            TENANT,
            "--approved-quote",
            q["quote_id"],
            "--resume-from",
            str(old),
            "--phases",
            "c",
            "--progress",
            str(progress),
            "--accept-documented-prices",
            "--state-dir",
            str(tmp_path / "state"),
            *extra,
        ],
        emit=lines.append,
    )
    return code, lines


def test_resume_refused_when_old_meta_is_missing(tmp_path: Path) -> None:
    q = _quote(tmp_path, "--phases", "c")
    code, lines = _prepare_resume(tmp_path, _old(tmp_path, q, None), "c", "--release-tag", "rel-1")
    assert code == 2 and "meta.json missing" in lines[-1]


def test_resume_without_release_tag_refused_even_when_both_sides_are_empty(tmp_path: Path) -> None:
    q = _quote(tmp_path, "--phases", "c")
    meta = {
        "release_tag": None,
        "script_commit": phases.git_commit(),
        "recorded_at": "2026-11-13T08:00:00+00:00",
    }
    code, lines = _prepare_resume(tmp_path, _old(tmp_path, q, meta), "c")
    assert code == 2 and "--resume-from needs --release-tag" in lines[-1]
    # 拒在搬记录之前:新目录里什么都没写
    assert not (tmp_path / "new" / "c" / "records.jsonl").exists()


def test_extraction_phases_take_no_release_tag(tmp_path: Path) -> None:
    q = _quote(tmp_path, "--phases", "c")
    meta = {
        "release_tag": "rel-1",
        "script_commit": phases.git_commit(),
        "recorded_at": "2026-11-13T08:00:00+00:00",
    }
    code, lines = _prepare_resume(tmp_path, _old(tmp_path, q, meta), "a", "--release-tag", "rel-1")
    assert code == 2 and "no --release-tag" in lines[-1]
