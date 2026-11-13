"""B-300 评测的段落、单元、文件与控制面那几步的输入(从 ``b300_eval`` 拆出,不花钱)。"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import b300_live as live  # noqa: E402
import b300_plan as plan  # noqa: E402
import b300_scenarios as sc  # noqa: E402

INPOD_SOURCE = _HERE / "b300_inpod.py"
_EMBED_LINE = "EMBEDDED_INPUT: Mapping[str, Any] | None = None"
#: 补跑上限:每段按两成(§8.6「补跑按两成算」),超了停下问。
RERUN_SHARE = 0.2


class EvalError(RuntimeError):
    """前提不成立 —— 停下,不猜。"""


@dataclass(frozen=True)
class Phase:
    name: str
    classes: tuple[str, ...]
    kind: str  # extraction | conversation | c4 | c5
    baseline: bool
    #: 只跑一家:``cut2`` / ``cut3`` 在对应删减生效时只跑一家;``always`` 总是一家(基线证明会红)。
    single: str = ""


PHASES: dict[str, Phase] = {
    p.name: p
    for p in (
        Phase("a", sc.A_CLASSES, "extraction", False),
        Phase("compare-before", ("D",), "extraction", True),
        Phase("compare-after", ("D",), "extraction", False),
        Phase("b", sc.B_CLASSES, "conversation", False),
        Phase("b-screen", ("B1", "B2"), "conversation", True, single="cut2"),
        Phase("c", ("C1", "C2", "C3"), "conversation", False, single="cut3"),
        Phase("c4", ("C4",), "c4", False),
        Phase("c5", ("C5",), "c5", False),
        Phase("c2-baseline", ("C2",), "conversation", True, single="always"),
        Phase("c4-baseline", ("C4",), "c4", True, single="always"),
        Phase("c5-baseline", ("C5",), "c5", True, single="always"),
    )
}


#: 记账用的 phase → 分预算的段(审查 M4,先后 B → C → A,见 ``b300_plan.BUDGET_ORDER``)。
PHASE_BUDGET = {
    "b": "B",
    "b-screen": "B",
    "calibration": "B",
    "c": "C",
    "c4": "C",
    "c5": "C",
    "c2-baseline": "C",
    "c4-baseline": "C",
    "c5-baseline": "C",
    "a": "A",
    "compare-before": "A",
    "compare-after": "A",
    "calibration-a": "A",
}


def budget_of(phase: str, *, pilot: bool = False) -> str:
    return "pilot" if pilot else PHASE_BUDGET.get(phase, "B")


def scenarios_for(phase: Phase) -> list[sc.Scenario]:
    if phase.classes == ("D",):
        return sc.build_compare()
    return [s for s in sc.build_all() if s.cls in phase.classes]


def all_scenarios() -> dict[str, sc.Scenario]:
    return sc.by_id([*sc.build_all(), *sc.build_compare()])


def phase_vendors(phase: Phase, q: Mapping[str, Any]) -> list[str]:
    everyone = [v["provider"] for v in q["vendors"]]
    cuts = {c["step"] for c in q["cuts_applied"]}
    if phase.single == "always" or (phase.single == "cut2" and 2 in cuts):
        return [q["single_vendor"]]
    return everyone


def vendors_for_class(phase: Phase, cls: str, q: Mapping[str, Any]) -> list[str]:
    cuts = {c["step"] for c in q["cuts_applied"]}
    if phase.single == "cut3" and 3 in cuts and cls in {"C1", "C3"}:
        return [q["single_vendor"]]
    return phase_vendors(phase, q)


# --------------------------------------------------------------------------- 单元


def user_name(
    quote_id: str, phase: str, scenario_id: str, vendor: str | None, repeat: int, attempt: int
) -> str:
    who = f"-{vendor}" if vendor else ""
    return f"b300-{quote_id[:6]}-{phase}-{scenario_id}{who}-r{repeat}-a{attempt}".lower()


def pilot_scenarios(phase: Phase) -> list[sc.Scenario]:
    """试点每段 :data:`b300_plan.PILOT_UNITS_PER_PHASE` 个场景:尽量取不同的类,各取第一个。"""
    rows = scenarios_for(phase)
    firsts = [next(s for s in rows if s.cls == cls) for cls in dict.fromkeys(s.cls for s in rows)]
    picked = firsts[: plan.PILOT_UNITS_PER_PHASE]
    for s in rows:  # 只有一类的段(C4 / C5 / 对比)补同类的下一个
        if len(picked) >= plan.PILOT_UNITS_PER_PHASE:
            break
        if s not in picked:
            picked.append(s)
    return picked


def build_units(phase: Phase, q: Mapping[str, Any]) -> list[dict[str, Any]]:
    """一段的全部单元(第 0 次尝试)。C5 的种子 / 整理按用户(场景 x 次),不分家;提问分家。
    试点报价(``q["pilot"]``)只取 :func:`pilot_scenarios`、一家、每个 1 次。"""
    units = []
    pilot = bool(q.get("pilot"))
    models = {v["provider"]: {"main": v["main"], "memory": v["memory"]} for v in q["vendors"]}
    for s in pilot_scenarios(phase) if pilot else scenarios_for(phase):
        for repeat in range(1 if pilot else plan.REPEATS):
            vendors = [q["single_vendor"]] if pilot else vendors_for_class(phase, s.cls, q)
            for vendor in vendors:
                shared = phase.kind == "c5"
                units.append(
                    {
                        "phase": phase.name,
                        "scenario_id": s.scenario_id,
                        "cls": s.cls,
                        "domain": s.domain,
                        "vendor": vendor,
                        "repeat": repeat,
                        "attempt": 0,
                        # 审查第五轮 M1:每个单元(及它的记录)带上型号与场景摘要,接着做时逐条核对
                        "models": models[vendor],
                        "scenario_digest": sc.scenario_digest([s]),
                        "user": user_name(
                            q["quote_id"],
                            phase.name,
                            s.scenario_id,
                            None if shared else vendor,
                            repeat,
                            0,
                        ),
                    }
                )
    return units


def unit_key(u: Mapping[str, Any]) -> str:
    return f"{u['phase']}|{u['scenario_id']}|{u['vendor']}|r{u['repeat']}|a{u['attempt']}"


def agent_code(provider: str, *, writeback: bool = False) -> str:
    return f"{live.EVAL_AGENT_PREFIX}{provider}{'-wb' if writeback else ''}"


# --------------------------------------------------------------------------- 目录与文件


def phase_dir(out: Path, phase: str) -> Path:
    path = out / phase
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def append_jsonl(path: Path, row: Mapping[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def save_units(
    out: Path, phase: str, units: Sequence[Mapping[str, Any]], q: Mapping[str, Any]
) -> Path:
    """单元绑定到报价(审查 M-c):``units.json`` 里记报价编号与是不是试点。"""
    path = phase_dir(out, phase) / "units.json"
    body = {"quote_id": q["quote_id"], "pilot": bool(q.get("pilot")), "units": list(units)}
    path.write_text(json.dumps(body, indent=1, ensure_ascii=False), encoding="utf-8")
    return path


def load_units(out: Path, phase: str, quote_id: str | None = None) -> list[dict[str, Any]]:
    """读单元;给了 ``quote_id`` 就核对:别的报价备的单元不跑(试点的单元只能用试点的报价跑)。"""
    path = out / phase / "units.json"
    if not path.exists():
        raise EvalError(f"{path} missing: run `prepare --phase {phase}` first")
    body = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(body, dict) or "quote_id" not in body:
        raise EvalError(f"{path} is not bound to a quote; prepare it again")
    if quote_id is not None and body["quote_id"] != quote_id:
        raise EvalError(
            f"{path} was prepared under quote {body['quote_id']} (pilot={body['pilot']}), "
            f"not {quote_id}; prepare a new --out for this quote"
        )
    return list(body["units"])


def inpod_rows(out: Path, phase: str, op: str, step: int | None = None) -> list[dict[str, Any]]:
    suffix = f"-step{step}" if step is not None else ""
    return read_jsonl(out / phase / f"inpod-{op}{suffix}.jsonl")


def latest(rows: Sequence[Mapping[str, Any]], key: str) -> dict[str, Mapping[str, Any]]:
    return {str(r[key]): r for r in rows if key in r}


# --------------------------------------------------------------------------- 控制面里的那几步


def bundle_source(payload: Mapping[str, Any], source: str | None = None) -> str:
    """把 ``b300_inpod.py`` 与这一步的输入拼成一个自包含文件(输入走 stdin 喂进 pod,不进参数)。"""
    text = INPOD_SOURCE.read_text(encoding="utf-8") if source is None else source
    if text.count(_EMBED_LINE) != 1:
        raise EvalError("b300_inpod.py no longer has exactly one EMBEDDED_INPUT line")
    literal = json.dumps(json.dumps(payload, ensure_ascii=False), ensure_ascii=False)
    return text.replace(
        _EMBED_LINE, f"EMBEDDED_INPUT: Mapping[str, Any] | None = json.loads({literal})"
    )


def seed_payload(
    phase: Phase, units: Sequence[Mapping[str, Any]], tenant_id: str
) -> dict[str, Any]:
    scenarios = all_scenarios()
    entries, seen = [], set()
    for u in units:
        s = scenarios[u["scenario_id"]]
        if not s.seeds or u["user"] in seen:
            continue
        seen.add(u["user"])
        entries.append(
            {
                "key": u["user"],
                "tenant_id": tenant_id,
                "user": u["user"],
                "agent_name": agent_code(u["vendor"]),
                "seeds": [asdict(seed) for seed in s.seeds],
            }
        )
    return {"op": "seed", "entries": entries}


def users_payload(
    op: str, units: Sequence[Mapping[str, Any]], tenant_id: str, **extra: Any
) -> dict[str, Any]:
    scenarios = all_scenarios()
    users, seen = [], set()
    for u in units:
        if u["user"] in seen:
            continue
        seen.add(u["user"])
        s = scenarios[u["scenario_id"]]
        users.append(
            {
                "key": u["user"],
                "tenant_id": tenant_id,
                "user": u["user"],
                "x_key": s.x_key,
                "y_key": s.y_key,
            }
        )
    return {"op": op, "users": users, **extra}


def writeback_usage_payload(
    units: Sequence[Mapping[str, Any]],
    tenant_id: str,
    since: str,
    run_ids: Mapping[str, Sequence[str]],
) -> dict[str, Any]:
    """C4 的带写回对话之后:每个评测用户的后台写回实际用量与写回任务状态(审查 H4 / HIGH)。
    ``since`` 是这一段最早一次 run 的开始时间;``run_ids`` 是每个用户带写回的那几次运行。"""
    users, seen = [], set()
    for u in units:
        if u["user"] in seen:
            continue
        seen.add(u["user"])
        users.append(
            {
                "key": u["user"],
                "tenant_id": tenant_id,
                "user": u["user"],
                "agent_name": agent_code(u["vendor"], writeback=True),
                "run_ids": [r for r in run_ids.get(u["user"], []) if r],
            }
        )
    return {"op": "writeback_usage", "since": since, "users": users}


def recalled_payload(out: Path, phase: str, tenant_id: str) -> dict[str, Any]:
    run_ids = [
        c["run_id"]
        for r in read_jsonl(out / phase / "records.jsonl")
        for c in r.get("convs", [])
        if c.get("run_id")
    ]
    return {"op": "recalled", "tenant_id": tenant_id, "run_ids": sorted(set(run_ids))}


# --------------------------------------------------------------------------- 版本记录


def git_commit() -> str | None:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],  # noqa: S607
            cwd=_HERE,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def release_meta(phase: Phase, release_tag: str | None) -> dict[str, Any]:
    """这一段测的是哪一版:对话段 = 当时测试环境上线的发布(``--release-tag``,如 ``f2a21c6b2`` 或
    ``f2a21c6b2-test``,提交号取它的十六进制前缀);提炼段 = 跑脚本的这个检出目录本身。"""
    if phase.kind != "extraction" and not release_tag:
        raise EvalError("--release-tag is required: record which test-env release this run hits")
    match = re.match(r"^([0-9a-f]{7,40})", release_tag or "")
    return {
        "phase": phase.name,
        "baseline": phase.baseline,
        "release_tag": release_tag,
        "release_commit": match.group(1) if match else None,
        "script_commit": git_commit(),
        "recorded_at": datetime.now(UTC).isoformat(),
    }


def write_meta(
    out: Path, phase: Phase, meta: Mapping[str, Any], *, step: int | None = None
) -> None:
    """记版本;**每次 run 的开始时间都留着、从不覆盖**(审查 HIGH:写回用量从最早那一步算起)。"""
    path = out / phase.name / "meta.json"
    old = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    if old and (old.get("release_tag"), old.get("script_commit")) != (
        meta["release_tag"],
        meta["script_commit"],
    ):
        raise EvalError(f"{path} records another release / commit {old}; use another --out")
    starts = list((old or {}).get("starts") or [])
    starts.append({"step": step, "at": meta["recorded_at"]})
    body = {**(old or {}), **meta, "recorded_at": (old or meta)["recorded_at"], "starts": starts}
    path.write_text(json.dumps(body, indent=2, ensure_ascii=False), encoding="utf-8")


def earliest_start(out: Path, phase: str) -> str:
    """这一段最早一次 run 的开始时间(ISO);没有就报错。"""
    path = out / phase / "meta.json"
    if not path.exists():
        raise EvalError(f"{path} missing: run the phase first")
    meta = json.loads(path.read_text(encoding="utf-8"))
    times = [meta["recorded_at"], *(s["at"] for s in meta.get("starts") or [])]
    return min(datetime.fromisoformat(t) for t in times).isoformat()


#: ``--user-said`` / ``--unblock`` 太短、只是「好」「ok」之类的不收(审查 LOW)。
_TRIVIAL = frozenset({"yes", "okay", "sure", "fine", "go on", "好的", "可以", "继续"})


def meaningful_text(raw: str) -> str:
    text = raw.strip()
    if len(text) < 4 or text.lower() in _TRIVIAL:
        raise ValueError("give the actual words / reason (at least 4 characters, not just 'ok')")
    return text


Slot = tuple[str, str, int]  # (场景, 家, 次)


def _slot(u: Mapping[str, Any]) -> Slot:
    return (str(u["scenario_id"]), str(u["vendor"]), int(u["repeat"]))


def finished_records(out: Path, name: str) -> list[dict[str, Any]]:
    """一段里做完了的记录(同一单元留最后一条):没出错、对话也齐了(提炼段 = 有结果)。"""
    phase, scenarios = PHASES[name], all_scenarios()
    latest_rows = {r["key"]: r for r in read_jsonl(out / name / "records.jsonl")}
    done = []
    for r in latest_rows.values():
        if r.get("error"):
            continue
        if phase.kind == "extraction":
            ok = r.get("result") is not None
        else:
            ok = len(r.get("convs", [])) >= len(scenarios[r["scenario_id"]].conversations)
        if ok:
            done.append(r)
    return done


def progress(out: Path) -> dict[str, Any]:
    """哪些单元已经做完(审查第三 / 四轮 M2):每段做完的(场景, 家, 次),与它们的摘要。重报价
    (``quote --progress``)用它算这份报价意味着的单元里还剩哪些;``prepare --resume-from`` 用同一个
    函数、核对同一个摘要,建的正好是报价数的那些。"""
    done = {
        name: sorted({_slot(r) for r in finished_records(out, name)})
        for name in PHASES
        if (out / name / "records.jsonl").exists()
    }
    digest = hashlib.sha256(json.dumps(done, sort_keys=True).encode()).hexdigest()[:12]
    return {
        "from_out": str(out),
        "done": {k: [list(x) for x in v] for k, v in done.items()},
        "digest": digest,
    }


def remaining_units(
    phase: Phase, q: Mapping[str, Any], done: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """这份报价在这一段意味着的单元,去掉已经做完的(场景, 家, 次)。"""
    finished = {tuple(x) for x in done.get(phase.name, [])}
    return [u for u in build_units(phase, q) if _slot(u) not in finished]


def remaining_for(
    q_shape: Mapping[str, Any], done: Mapping[str, Any]
) -> Callable[[Sequence[int], str], tuple[dict[str, float], dict[str, int]]]:
    """给报价用:按某一组删减、某一家「只跑一家」时,每段还剩的比例与单元数。"""

    def compute(cuts: Sequence[int], single: str) -> tuple[dict[str, float], dict[str, int]]:
        q = {**q_shape, "single_vendor": single, "cuts_applied": [{"step": c} for c in cuts]}
        shares: dict[str, float] = {}
        counts: dict[str, int] = {}
        for name, phase in PHASES.items():
            if budget_of(name) not in q["phases"]:
                continue
            implied = build_units(phase, q)
            left = remaining_units(phase, q, done)
            counts[name] = len(left)
            shares[name] = round(len(left) / len(implied), 6) if implied else 1.0
        return shares, counts

    return compute


def check_carry(old_out: Path, name: str, q: Mapping[str, Any], release_tag: str | None) -> None:
    """接着做之前核对版本(审查第五轮 M1):旧目录这一段的发布版本、脚本提交要与这次一样;每条要搬的
    记录的型号与场景摘要要与这份报价、现在的场景一样。对不上就整段拒,不悄悄混。"""
    meta_file = old_out / name / "meta.json"
    rows = finished_records(old_out, name)
    if rows and not meta_file.exists():
        raise EvalError(
            f"{meta_file} missing: cannot tell which release the finished records ran on"
        )
    if meta_file.exists():
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        if meta.get("release_tag") != release_tag:
            raise EvalError(
                f"{name}: old records ran on release {meta.get('release_tag')!r}, this run is "
                f"{release_tag!r}; results from different releases are not merged"
            )
        if meta.get("script_commit") != git_commit():
            raise EvalError(
                f"{name}: old records were made by script commit {meta.get('script_commit')}, "
                f"this checkout is {git_commit()}; not merged"
            )
    models = {v["provider"]: {"main": v["main"], "memory": v["memory"]} for v in q["vendors"]}
    scenarios = all_scenarios()
    for r in rows:
        if r.get("models") != models.get(r["vendor"]):
            raise EvalError(
                f"{r['key']}: ran on models {r.get('models')}, this quote uses "
                f"{models.get(r['vendor'])}; not carried"
            )
        if r.get("scenario_digest") != sc.scenario_digest([scenarios[r["scenario_id"]]]):
            raise EvalError(f"{r['key']}: the scenario changed since it ran; not carried")


def carry_over(old_out: Path, new_out: Path, name: str, new_quote_id: str) -> int:
    """把旧目录里这一段做完的记录搬进新目录(``carried_from_quote`` 标原报价),每条在
    ``carried.jsonl`` 留一行审计;召回 / 状态 / 判分等结果文件整份带过来,报告才能合着算。"""
    rows = finished_records(old_out, name)
    if not rows:
        return 0
    units_file = old_out / name / "units.json"
    old_quote = (
        json.loads(units_file.read_text(encoding="utf-8")).get("quote_id")
        if units_file.exists()
        else None
    )
    target = phase_dir(new_out, name)
    at = datetime.now(UTC).isoformat()
    for r in rows:
        append_jsonl(
            target / "records.jsonl",
            {**r, "carried_from_quote": old_quote, "carried_from_out": str(old_out)},
        )
        append_jsonl(
            target / "carried.jsonl",
            {
                "key": r["key"],
                "from_quote": old_quote,
                "from_out": str(old_out),
                "to_quote": new_quote_id,
                "at": at,
            },
        )
    for src in [*sorted((old_out / name).glob("inpod-*.jsonl")), old_out / name / "verdicts.jsonl"]:
        if src.exists():
            with (target / src.name).open("a", encoding="utf-8") as fh:
                fh.write(src.read_text(encoding="utf-8"))
    if (old_out / name / "meta.json").exists():
        (target / "carried-meta.json").write_text(
            (old_out / name / "meta.json").read_text(encoding="utf-8")
        )
    return len(rows)


def calibration_path(out: Path, which: str) -> Path:
    """两套校准(审查 M3):``b`` = B / C 组回答(3 票);``a`` = A 组提炼(按报价的 A 组票数)。"""
    return out / ("calibration.json" if which == "b" else "calibration-a.json")


def calibration_ok(out: Path, which: str = "b", *, votes: int | None = None) -> bool:
    path = calibration_path(out, which)
    if not path.exists():
        return False
    data = json.loads(path.read_text(encoding="utf-8"))
    return (
        data.get("judge_model") == plan.JUDGE_MODEL
        and (votes is None or int(data.get("votes", 0)) == votes)
        and int(data.get("agree", 0)) >= plan.CALIBRATION_MIN_AGREE
    )


def record_approval(out: Path, *, cmd: str, quote_id: str, user_said: str | None) -> None:
    """用户在 ``stop_and_ask`` 时说「跑」的原话,每个花钱的命令都记一行(审查 LOW)。"""
    if not user_said:
        return
    out.mkdir(parents=True, exist_ok=True)
    append_jsonl(
        out / "approvals.jsonl",
        {
            "cmd": cmd,
            "quote_id": quote_id,
            "user_said": user_said,
            "at": datetime.now(UTC).isoformat(),
        },
    )


def check_labels(labels: Sequence[Mapping[str, Any]]) -> None:
    """§8.3:20 条人工标注,5 个领域各 4 条,是 / 否各半。标注由人给,脚本不造。"""
    if len(labels) != plan.CALIBRATION_ITEMS:
        raise EvalError(f"calibration wants {plan.CALIBRATION_ITEMS} labels, got {len(labels)}")
    per_domain = defaultdict(int)
    for row in labels:
        per_domain[row["domain"]] += 1
    if sorted(per_domain) != sorted(sc.DOMAINS) or set(per_domain.values()) != {4}:
        raise EvalError(f"calibration labels must be 4 per domain: {dict(per_domain)}")
    if sum(bool(r["label"]) for r in labels) * 2 != len(labels):
        raise EvalError("calibration labels must be half yes, half no")
