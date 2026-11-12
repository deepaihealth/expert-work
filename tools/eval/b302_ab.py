"""B-302 PR 2 A/B —— 运行脚本(设计 §5.3 任务 / 组别 / 次数 / 先后,§5.5 报价与止损)。

``docs/superpowers/specs/2026-11-11-b302-runaway-reverify-design.md``。**会花钱**:``run`` 子命令调
真模型,必须带用户批过的报价编号(``--approved-quote``)和止损线(``--stop-line``,元),否则拒跑。
其余子命令全部离线:

    python tools/eval/b302_ab.py plan --docs OUT --doubao-model <§6.2 查到的型号>
    python tools/eval/b302_ab.py manifests --out AGENTS --doubao-model <...>
    python tools/eval/b302_ab.py run --phase pilot|arm_a|rest --docs OUT --out RECORDS \\
        --doubao-model <...> --stop-line 650 --approved-quote <报价编号>
    python tools/eval/b302_ab.py report --docs OUT --out RECORDS

* 组别:A = 今天的行为;B = 只开第 1 层;C = 第 1 层 + 第 2 层(N=3)。三个临时智能体只差
  ``policies.reading_rule`` / ``policies.figure_view_steps``(:func:`write_manifests`,控制台导入)。
* 先后:各模型试点(1 份 x 3 轮,只 A 组)→ 各模型 A 组 → 判复现前提后再跑 B、C 与回归集。
  试点之后人工按 §5.3 查 ``token_usage`` 的全部用量种类(``conversation`` 以外不为 0 就停)。
* 止损:每跑完一轮按 ``end`` 帧的 ``usage_by_model`` 累计实际花费,到线不再派新会话;单次调用输入
  超过 25 万 token 就取消这次运行(:class:`TurnCollector`)。每家并发 2 路。
* 只走对外平面 ``/v1/agents/{code}/...``(凭据:``EXPERT_WORK_EVAL_API_KEY``)。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import statistics
import sys
import time
import zipfile
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Any

import yaml

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import b302_metrics as metrics  # noqa: E402
from behavior_client import SseParser, message_text  # noqa: E402

ARMS: dict[str, dict[str, Any]] = {
    "A": {"reading_rule": False, "figure_view_steps": 0},
    "B": {"reading_rule": True, "figure_view_steps": 0},
    "C": {"reading_rule": True, "figure_view_steps": 3},
}
VIEW_STEPS = 3
#: 单次调用输入超过它就取消这次运行(§5.5 第 3 条)。
MAX_INPUT_TOKENS = 250_000
CONCURRENCY_PER_VENDOR = 2
#: 一轮的 HTTP 读超时:时限 90 分钟 + 收尾余量。
RUN_TIMEOUT_S = 7200.0
#: §5.5 的报价口径:每次运行 15 步、每步 3 万输入 / 3,000 输出、无压缩。
EXPECTED_STEPS, EXPECTED_IN, EXPECTED_OUT = 15, 30_000, 3_000
#: 每次运行的上限(百万 token):主循环 356.6 万(全按输出价)、压缩 650 万输入 + 85.2 万输出、
#: 超 25 万的 2 次 206.6 万(全按输出价)。
CAP_MAIN_M, CAP_SUMMARY_IN_M, CAP_SUMMARY_OUT_M, CAP_OVERSIZE_M = 3.566, 6.5, 0.852, 2.066
MAX_ITERATIONS, MAX_COMPRESSIONS = 40, 26
DELIVERABLE_EXT = {"slides": ".pptx", "spreadsheet": ".xlsx", "document": ".docx"}
_WRITERS = frozenset({"write_file", "edit_file", "exec_python", "bash", "save_artifact"})


class PlanError(RuntimeError):
    """计划 / 报价 / 计费的前提不成立 —— 一律停下,不猜。"""


@dataclass(frozen=True)
class ModelPlan:
    name: str
    provider: str
    arms: tuple[str, ...]
    #: 元 / 百万 token:输入 / 输出 / 缓存读。
    price: tuple[float, float, float]
    regression: bool = False


def default_models(doubao_model: str) -> list[ModelPlan]:
    """§5.3 + D9:glm-5.3-flash(事故模型,另跑回归集)、deepseek-flash 三组;豆包只 A、C 两组。
    单价取自设计 §5.5 / §5.7 引的价目表。豆包型号以 §6.2 的查询为准,由调用方给。"""
    return [
        ModelPlan("glm-5.3-flash", "glm", ("A", "B", "C"), (0.8, 2.8, 0.23), regression=True),
        ModelPlan("deepseek-flash", "deepseek", ("A", "B", "C"), (2.0, 8.0, 0.04)),
        ModelPlan(doubao_model, "doubao", ("A", "C"), (6.0, 30.0, 1.2)),
    ]


@dataclass(frozen=True)
class Session:
    """一个会话:同一份文档在同一组里的几轮(每轮是一次运行)。"""

    phase: str  # pilot | main | regression
    model: str
    arm: str
    doc_id: str
    turns: int
    repeat: int = 0


def phase_of(session: Session) -> str:
    if session.phase == "pilot":
        return "pilot"
    return "arm_a" if session.phase == "main" and session.arm == "A" else "rest"


def load_docs(docs_dir: Path) -> list[dict[str, Any]]:
    manifest = json.loads((docs_dir / "manifest.json").read_text(encoding="utf-8"))
    if not manifest.get("complete"):
        raise PlanError("document set has no incident category (--incident-template); refused")
    return [
        json.loads((docs_dir / f"{d}.gold.json").read_text(encoding="utf-8"))
        for d in manifest["docs"]
    ]


def _regression_ids(docs_dir: Path | None) -> list[str]:
    if docs_dir is None:
        return ["regression-4p", "regression-6p"]
    return list(json.loads((docs_dir / "manifest.json").read_text())["regression"])


def plan(
    docs: Sequence[Mapping[str, Any]], models: Sequence[ModelPlan], docs_dir: Path | None = None
) -> list[Session]:
    """§5.3 的全部会话,按先后排:试点 → A 组 → B、C 与回归集。"""
    pilot = [Session("pilot", m.name, "A", str(docs[0]["doc_id"]), 3) for m in models]
    main = [
        Session("main", m.name, arm, str(d["doc_id"]), 3 if d.get("correction") else 2)
        for m in models
        for arm in m.arms
        for d in docs
    ]
    regression = [
        Session("regression", m.name, arm, rid, 1, repeat)
        for m in models
        if m.regression
        for arm in m.arms
        for rid in _regression_ids(docs_dir)
        for repeat in range(2)
    ]
    sessions = [*pilot, *main, *regression]
    order = ["pilot", "arm_a", "rest"]
    return sorted(sessions, key=lambda s: order.index(phase_of(s)))


def runs_per_model(sessions: Iterable[Session]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for s in sessions:
        counts[s.model] += s.turns
    return dict(counts)


def run_cap_yuan(price: tuple[float, float, float]) -> float:
    inp, out, _cache = price
    return (
        CAP_MAIN_M * out + CAP_SUMMARY_IN_M * inp + CAP_SUMMARY_OUT_M * out + CAP_OVERSIZE_M * out
    )


def run_expected_yuan(price: tuple[float, float, float]) -> float:
    inp, out, _cache = price
    return EXPECTED_STEPS * (EXPECTED_IN * inp + EXPECTED_OUT * out) / 1e6


def hard_ceiling_yuan(stop_line: float, models: Sequence[ModelPlan]) -> float:
    """实际花费硬上限:止损线 + 每家同时在跑的 2 路各按每次上限算。"""
    return stop_line + sum(CONCURRENCY_PER_VENDOR * run_cap_yuan(m.price) for m in models)


def call_estimate(sessions: Sequence[Session]) -> dict[str, int]:
    runs = sum(s.turns for s in sessions)
    return {
        "runs": runs,
        "expected_calls": runs * EXPECTED_STEPS,
        "max_calls": runs * (MAX_ITERATIONS + 2 + MAX_COMPRESSIONS),
    }


def quote(
    sessions: Sequence[Session], models: Sequence[ModelPlan], stop_line: float
) -> dict[str, Any]:
    runs = runs_per_model(sessions)
    by_model = {
        m.name: {
            "runs": runs.get(m.name, 0),
            "expected_yuan": round(runs.get(m.name, 0) * run_expected_yuan(m.price), 2),
            "theoretical_cap_yuan": round(runs.get(m.name, 0) * run_cap_yuan(m.price), 2),
        }
        for m in models
    }
    return {
        "models": by_model,
        "expected_total_yuan": round(sum(v["expected_yuan"] for v in by_model.values()), 2),
        "theoretical_total_yuan": round(
            sum(v["theoretical_cap_yuan"] for v in by_model.values()), 2
        ),
        "stop_line_yuan": stop_line,
        "hard_ceiling_yuan": round(hard_ceiling_yuan(stop_line, models), 2),
        **call_estimate(sessions),
    }


# --------------------------------------------------------------------------- spend


@dataclass
class SpendTracker:
    stop_line: float
    prices: Mapping[str, tuple[float, float, float]]
    spent: float = 0.0

    def add(self, usage_by_model: Iterable[Mapping[str, Any]]) -> float:
        cost = 0.0
        for bucket in usage_by_model:
            model = str(bucket.get("model"))
            if model not in self.prices:
                raise PlanError(f"no price for model {model!r}; stop and quote again")
            inp, out, cache = self.prices[model]
            details = bucket.get("input_token_details") or {}
            cached = int(details.get("cache_read") or 0)
            total_in = int(bucket.get("input_tokens") or 0)
            cost += (
                (total_in - cached) * inp
                + cached * cache
                + int(bucket.get("output_tokens") or 0) * out
            ) / 1e6
        self.spent += cost
        return cost

    def may_start(self) -> bool:
        return self.spent < self.stop_line


# --------------------------------------------------------------------------- frames


@dataclass
class TurnCollector:
    """把一轮的 SSE 帧累积成 :class:`b302_metrics.Step`,并盯住单次调用的输入上限。"""

    max_input_tokens: int = MAX_INPUT_TOKENS
    run_id: str | None = None
    steps: list[metrics.Step] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    tripped: list[str] = field(default_factory=list)
    end: dict[str, Any] | None = None
    cancel_reason: str | None = None
    input_tokens: list[int] = field(default_factory=list)

    def on_frame(self, event: str, raw: str) -> None:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return
        if not isinstance(data, dict):
            return
        if event == "metadata":
            self.run_id = data.get("run_id") or self.run_id
        elif event == "guard":
            target = self.warnings if data.get("kind") == "warning" else self.tripped
            target.append(str(data.get("guard")))
        elif event == "end":
            self.end = data
        elif event == "updates":
            for node in data.values():
                for msg in (node or {}).get("messages") or [] if isinstance(node, dict) else []:
                    if isinstance(msg, dict) and msg.get("type") in {"ai", "AIMessage"}:
                        self._on_ai(msg)

    def _on_ai(self, msg: Mapping[str, Any]) -> None:
        usage = msg.get("usage_metadata") or {}
        kwargs = msg.get("additional_kwargs") or {}
        calls = tuple(
            metrics.Call(
                str(c.get("name", "")), c.get("args") if isinstance(c.get("args"), dict) else {}
            )
            for c in msg.get("tool_calls") or []
            if isinstance(c, dict)
        )
        details = usage.get("output_token_details") or {}
        self.steps.append(
            metrics.Step(
                index=len(self.steps),
                thinking=str(kwargs.get("reasoning_content") or ""),
                reply=message_text(msg.get("content")),
                calls=calls,
                reasoning_tokens=details.get("reasoning"),
            )
        )
        tokens = int(usage.get("input_tokens") or 0)
        self.input_tokens.append(tokens)
        if tokens > self.max_input_tokens and self.cancel_reason is None:
            self.cancel_reason = f"one call had {tokens} input tokens (> {self.max_input_tokens})"


def first_deliverable_step(steps: Sequence[metrics.Step], deliverable: str) -> int | None:
    """第一次产出所要类型文件的那一步(近似:写文件类调用的参数里第一次出现那个扩展名)。"""
    ext = DELIVERABLE_EXT[deliverable]
    for step in steps:
        for call in step.calls:
            if call.name in _WRITERS and ext in json.dumps(call.args, ensure_ascii=False):
                return step.index
    return None


# --------------------------------------------------------------------------- deliverables


def _xml_text(body: str) -> list[str]:
    return [t for t in re.findall(r"<(?:a|w|x)?:?t(?:\s[^>]*)?>([^<]*)</", body) if t.strip()]


def deliverable_text(data: bytes) -> str:
    """OOXML(pptx / xlsx / docx)里的全部文字,按文件次序;只用标准库。"""
    with zipfile.ZipFile(BytesIO(data)) as z:
        names = sorted(
            (n for n in z.namelist() if n.endswith(".xml")),
            key=lambda n: [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", n)],
        )
        parts: list[str] = []
        for name in names:
            parts.extend(_xml_text(z.read(name).decode("utf-8", "replace")))
    return " ".join(parts)


def deliverable_structure(data: bytes, deliverable: str) -> int:
    """判据 9 的「页数 / 表数 / 一级标题数」。"""
    with zipfile.ZipFile(BytesIO(data)) as z:
        names = z.namelist()
        if deliverable == "slides":
            return sum(bool(re.fullmatch(r"ppt/slides/slide\d+\.xml", n)) for n in names)
        if deliverable == "spreadsheet":
            return sum(bool(re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n)) for n in names)
        body = z.read("word/document.xml").decode("utf-8", "replace")
        return len(re.findall(r'<w:pStyle w:val="(?:Heading1|1|heading 1)"', body))


def template_followed(text: str, titles: Sequence[str]) -> bool:
    norm = metrics.normalize(text)
    pos = 0
    for title in titles:
        found = norm.find(metrics.normalize(title), pos)
        if found < 0:
            return False
        pos = found + 1
    return True


# --------------------------------------------------------------------------- manifests


def manifest(model: ModelPlan, arm: str) -> dict[str, Any]:
    """§5.3 的临时智能体;三组只差 ``ARMS`` 里的两个字段。"""
    return {
        "apiVersion": "expert_work.io/v1",
        "kind": "Agent",
        "metadata": {"name": agent_code(model.name, arm), "version": "1.0.0", "tenant": "b302-ab"},
        "spec": {
            "description": "B-302 PR 2 A/B temporary agent (synthetic documents only).",
            "tenant_config": {"isolation_level": "dedicated_sandbox"},
            "model": {
                "provider": model.provider,
                "name": model.name,
                "supports_vision": True,
                "max_tokens": 32_768,
            },
            "system_prompt": {
                "template": (
                    "You are a general office assistant working in a workspace. Read the user's "
                    "files, write files and run scripts to do what the user asks."
                )
            },
            "workflow": {"type": "react", "max_iterations": MAX_ITERATIONS},
            "dynamic_workers": {"enabled": False},
            "policies": {"run_deadline_s": 5400, "token_budget": 3_000_000, **ARMS[arm]},
            "sandbox": {
                "runtime": "gvisor",
                "resources": {"cpu": "0.5", "memory": "512Mi"},
                "network": {"egress": "none"},
                "filesystem": {
                    "readonly_root": True,
                    "writable": ["/tmp"],  # noqa: S108 - the sandbox scratch dir, not a host path
                    "persistent_workspace": True,
                },
            },
        },
    }


def agent_code(model: str, arm: str) -> str:
    return "eval-b302-" + re.sub(r"[^a-z0-9]+", "-", model.lower()).strip("-") + "-" + arm.lower()


def write_manifests(out: Path, models: Sequence[ModelPlan]) -> dict[tuple[str, str], Path]:
    out.mkdir(parents=True, exist_ok=True)
    paths: dict[tuple[str, str], Path] = {}
    for model in models:
        for arm in model.arms:
            path = out / f"{agent_code(model.name, arm)}.yaml"
            path.write_text(
                yaml.safe_dump(manifest(model, arm), allow_unicode=True, sort_keys=False)
            )
            paths[(model.name, arm)] = path
    return paths


# --------------------------------------------------------------------------- running (paid)

_USER = "b302-ab"


async def _upload(
    http: Any, agent: str, name: str, data: bytes, session_id: str | None
) -> tuple[str, str]:
    content_type = "application/pdf" if name.endswith(".pdf") else "text/markdown"
    form = {"user_id": _USER, **({"session_id": session_id} if session_id else {})}
    resp = await http.post(
        f"/v1/agents/{agent}/uploads", data=form, files={"file": (name, data, content_type)}
    )
    resp.raise_for_status()
    body = resp.json()["data"]
    return str(body["upload_id"]), str(body["session_id"])


async def _turn(
    http: Any, agent: str, session_id: str, prompt: str, uploads: list[str], tracker: SpendTracker
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "user_id": _USER,
        "input": prompt,
        "mode": "stream",
        "session_id": session_id,
    }
    if uploads:
        payload["files"] = [{"upload_id": u} for u in uploads]
    collector, parser = TurnCollector(), SseParser()
    started = time.monotonic()
    cancelled = False
    async with http.stream("POST", f"/v1/agents/{agent}/runs", json=payload) as resp:
        resp.raise_for_status()
        async for line in resp.aiter_lines():
            frame = parser.feed(line)
            if frame is not None:
                collector.on_frame(*frame)
            if collector.cancel_reason and not cancelled and collector.run_id:
                cancelled = True
                await http.post(
                    f"/v1/agents/{agent}/runs/{collector.run_id}:cancel", json={"user_id": _USER}
                )
    tail = parser.flush()
    if tail is not None:
        collector.on_frame(*tail)
    end = collector.end or {}
    tracker.add(end.get("usage_by_model") or [])
    return {
        "run_id": collector.run_id,
        "wall_s": round(time.monotonic() - started, 1),
        "steps": [asdict(s) for s in collector.steps],
        "input_tokens": collector.input_tokens,
        "warnings": collector.warnings,
        "tripped": collector.tripped,
        "exit_reason": end.get("exit_reason"),
        "status": end.get("status"),
        "usage_by_model": end.get("usage_by_model"),
        "artifacts": end.get("artifacts") or [],
        "cancelled": collector.cancel_reason,
    }


async def _latest_deliverable(
    http: Any, agent: str, record: Mapping[str, Any], ext: str
) -> bytes | None:
    entries = [a for a in record["artifacts"] if str(a.get("name", "")).endswith(ext)]
    if not entries:
        return None
    entry = entries[-1]
    params = {
        "user_id": _USER,
        "name": entry["name"],
        **({"version": entry["version"]} if entry.get("version") is not None else {}),
    }
    resp = await http.get(f"/v1/agents/{agent}/artifacts/download", params=params)
    return resp.content if resp.status_code == 200 else None


async def run_session(
    http: Any,
    session: Session,
    *,
    doc: Mapping[str, Any],
    docs_dir: Path,
    agent: str,
    tracker: SpendTracker,
) -> dict[str, Any]:
    """一个会话:第 1 轮看要点 → 第 2 轮做交付件(另存一份,准确率按它算)→ 第 3 轮用户更正。"""
    pdf = (docs_dir / f"{session.doc_id}.pdf").read_bytes()
    upload_id, session_id = await _upload(http, agent, f"{session.doc_id}.pdf", pdf, None)
    ext = DELIVERABLE_EXT.get(str(doc.get("deliverable")), "")
    turns: list[dict[str, Any]] = []
    prompts: list[tuple[str, list[str]]] = [(str(doc["prompts"]["turn1"]), [upload_id])]
    if session.turns >= 2:
        extra: list[str] = []
        if doc.get("template"):
            name = f"{session.doc_id}.template.md"
            tid, _ = await _upload(http, agent, name, (docs_dir / name).read_bytes(), session_id)
            extra.append(tid)
        prompts.append((str(doc["prompts"]["turn2"]), extra))
    if session.turns >= 3 and "turn3" in doc["prompts"]:
        prompts.append((str(doc["prompts"]["turn3"]), []))
    try:
        for number, (prompt, uploads) in enumerate(prompts, 1):
            record = await _turn(http, agent, session_id, prompt, uploads, tracker)
            record["turn"] = number
            if number >= 2 and ext:
                data = await _latest_deliverable(http, agent, record, ext)
                record["deliverable_text"] = deliverable_text(data) if data else None
                record["deliverable_structure"] = (
                    deliverable_structure(data, str(doc["deliverable"])) if data else None
                )
            turns.append(record)
            if record.get("cancelled"):
                break
    finally:
        await http.delete(f"/v1/agents/{agent}/sessions/{session_id}", params={"user_id": _USER})
    return {**asdict(session), "turns": turns}


async def run_phase(
    sessions: Sequence[Session],
    *,
    docs_dir: Path,
    out: Path,
    base_url: str,
    api_key: str,
    tracker: SpendTracker,
    models: Sequence[ModelPlan],
    emit: Callable[[str], None] = print,
) -> None:
    """按家并发 2 路;到止损线不再派新会话。每个会话一行写进 ``out/records.jsonl``。"""
    import httpx

    docs = {d["doc_id"]: d for d in load_docs(docs_dir)}
    vendor = {m.name: m.provider for m in models}
    gates = {p: asyncio.Semaphore(CONCURRENCY_PER_VENDOR) for p in set(vendor.values())}
    out.mkdir(parents=True, exist_ok=True)
    sink = out / "records.jsonl"

    async with httpx.AsyncClient(
        base_url=base_url,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=httpx.Timeout(RUN_TIMEOUT_S),
    ) as http:

        async def _one(session: Session) -> None:
            async with gates[vendor[session.model]]:
                if not tracker.may_start():
                    emit(f"stop line reached ({tracker.spent:.2f}); skipped {session}")
                    return
                doc = docs.get(session.doc_id) or json.loads(
                    (docs_dir / f"{session.doc_id}.gold.json").read_text(encoding="utf-8")
                )
                if session.phase == "regression":
                    doc = {"prompts": {"turn1": doc["prompt"]}, "deliverable": None}
                record = await run_session(
                    http,
                    session,
                    doc=doc,
                    docs_dir=docs_dir,
                    agent=agent_code(session.model, session.arm),
                    tracker=tracker,
                )
                with sink.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(record, ensure_ascii=False) + "\n")
                emit(
                    f"done {session.model}/{session.arm}/{session.doc_id} spent={tracker.spent:.2f}"
                )

        await asyncio.gather(*(_one(s) for s in sessions))


# --------------------------------------------------------------------------- report


def _steps(turn: Mapping[str, Any]) -> tuple[metrics.Step, ...]:
    return tuple(
        metrics.Step(
            index=s["index"],
            thinking=s["thinking"],
            reply=s["reply"],
            calls=tuple(metrics.Call(c["name"], c["args"]) for c in s["calls"]),
            reasoning_tokens=s.get("reasoning_tokens"),
        )
        for s in turn["steps"]
    )


def doc_result(
    record: Mapping[str, Any], doc: Mapping[str, Any], *, view_steps: int
) -> metrics.DocResult:
    """一个会话(主实验)的汇总:第 2 轮为主,第 3 轮只看更正有没有照改。"""
    fields = [metrics.Field(f["tag"], f["label"], f["value"]) for f in doc["fields"]]
    name = f"{doc['doc_id']}.pdf"
    pages = {(name, i + 1): text for i, text in enumerate(doc["pages"])}
    turns = {t["turn"]: t for t in record["turns"]}
    second = turns.get(2) or {"steps": [], "exit_reason": None, "warnings": [], "wall_s": 0.0}
    steps = _steps(second)
    first_out = first_deliverable_step(steps, str(doc["deliverable"]))
    turn = metrics.Turn(
        doc_id=doc["doc_id"],
        steps=steps,
        exit_reason=second.get("exit_reason"),
        first_deliverable_step=first_out,
        warnings=tuple(second.get("warnings") or ()),
    )
    texts = [s.text for s in steps]
    thinking = [s.reasoning_tokens for s in steps if s.reasoning_tokens is not None]
    read_before = {
        c.args.get("path", "")
        for s in _steps(turns.get(1, {"steps": []}))
        for c in s.calls
        if c.name == "read_page"
    }
    deliverable = second.get("deliverable_text") or ""
    correction = doc.get("correction")
    third = turns.get(3)
    usage = second.get("usage_by_model") or []
    return metrics.DocResult(
        doc_id=doc["doc_id"],
        category=doc["category"],
        runaway=bool(metrics.runaway_reasons(turn, fields, pages)),
        correct=metrics.count_correct(deliverable, fields),
        flipped_fields=sum(metrics.changes(metrics.readings(texts, f)) >= 3 for f in fields),
        recopy_share=(sum(metrics.recopy_flags(steps, pages)) / len(steps)) if steps else 0.0,
        calls_after_deliverable=(len(steps) - 1 - first_out) if first_out is not None else None,
        thinking_median=float(statistics.median(thinking)) if thinking else None,
        refetches=metrics.refetches_after_retire(
            steps, view_steps=view_steps, read_earlier=read_before
        ),
        no_progress_exit=second.get("exit_reason") == "no_progress",
        wall_s=float(second.get("wall_s") or 0.0),
        tokens=sum(
            int(u.get("input_tokens") or 0) + int(u.get("output_tokens") or 0) for u in usage
        ),
        correction_applied=(
            metrics.normalize(correction["value"])
            in metrics.normalize(third.get("deliverable_text") or "")
            if correction and third
            else None
        ),
        template_ok=template_followed(deliverable, doc["template"])
        if doc.get("template")
        else None,
        structure=second.get("deliverable_structure"),
    )


def report(docs_dir: Path, out: Path) -> dict[str, Any]:
    docs = {d["doc_id"]: d for d in load_docs(docs_dir)}
    records = [
        json.loads(line) for line in (out / "records.jsonl").read_text().splitlines() if line
    ]
    arms: dict[str, dict[str, list[metrics.DocResult]]] = {}
    regression: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for rec in records:
        if rec["phase"] == "main":
            arms.setdefault(rec["model"], {}).setdefault(rec["arm"], []).append(
                doc_result(rec, docs[rec["doc_id"]], view_steps=VIEW_STEPS)
            )
        elif rec["phase"] == "regression":
            regression.setdefault(rec["model"], {}).setdefault(rec["arm"], []).append(rec)
    verdicts = {
        model: metrics.model_verdict(
            by_arm, regression_ok=_regression_ok(regression.get(model), docs_dir)
        )
        for model, by_arm in arms.items()
        if "A" in by_arm and "C" in by_arm
    }
    return {
        "verdicts": {m: asdict(v) for m, v in verdicts.items()},
        "landing": metrics.landing(verdicts),
    }


def _regression_ok(
    by_arm: Mapping[str, list[dict[str, Any]]] | None, docs_dir: Path
) -> bool | None:
    """判据 7(只 glm):B、C 都没有轮转、``read_page`` 次数 ≤ 页数 + 2、答对的个数不少于 A。"""
    if not by_arm:
        return None

    def _score(rec: Mapping[str, Any]) -> tuple[bool, bool, int]:
        gold = json.loads((docs_dir / f"{rec['doc_id']}.gold.json").read_text(encoding="utf-8"))
        steps = _steps(rec["turns"][0])
        calls = [list(units) for _i, _n, units in metrics.read_events(steps)]
        reply = metrics.normalize(" ".join(s.reply for s in steps))
        right = sum(str(r["total"]) in reply for r in gold["rows"])
        return (
            metrics.rotation(calls, pages=gold["pages"]),
            len(calls) <= gold["pages"] + 2,
            right,
        )

    scores = {arm: [_score(r) for r in recs] for arm, recs in by_arm.items()}
    a_right = sum(s[2] for s in scores.get("A", []))
    for arm in ("B", "C"):
        rows = scores.get(arm, [])
        if any(rot or not few for rot, few, _ in rows) or sum(s[2] for s in rows) < a_right:
            return False
    return True


# --------------------------------------------------------------------------- CLI


def main(argv: Sequence[str] | None = None, *, emit: Callable[[str], None] = print) -> int:
    parser = argparse.ArgumentParser(description="B-302 PR 2 A/B (design §5)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_plan = sub.add_parser("plan")
    p_plan.add_argument("--docs", type=Path, required=True)
    p_plan.add_argument("--doubao-model", required=True)
    p_plan.add_argument("--stop-line", type=float, default=650.0)
    p_man = sub.add_parser("manifests")
    p_man.add_argument("--out", type=Path, required=True)
    p_man.add_argument("--doubao-model", required=True)
    p_run = sub.add_parser("run")
    p_run.add_argument("--phase", choices=("pilot", "arm_a", "rest"), required=True)
    p_run.add_argument("--docs", type=Path, required=True)
    p_run.add_argument("--out", type=Path, required=True)
    p_run.add_argument("--doubao-model", default=None)
    p_run.add_argument("--stop-line", type=float, required=True)
    p_run.add_argument("--approved-quote", required=True)
    p_run.add_argument("--base-url", default=os.environ.get("EXPERT_WORK_EVAL_BASE_URL"))
    p_rep = sub.add_parser("report")
    p_rep.add_argument("--docs", type=Path, required=True)
    p_rep.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)

    if args.cmd == "plan":
        models = default_models(args.doubao_model)
        sessions = plan(load_docs(args.docs), models, args.docs)
        emit(json.dumps(quote(sessions, models, args.stop_line), indent=2, ensure_ascii=False))
        return 0
    if args.cmd == "manifests":
        for path in write_manifests(args.out, default_models(args.doubao_model)).values():
            emit(str(path))
        return 0
    if args.cmd == "report":
        emit(json.dumps(report(args.docs, args.out), indent=2, ensure_ascii=False))
        return 0
    api_key = os.environ.get("EXPERT_WORK_EVAL_API_KEY")
    if not args.doubao_model or not args.base_url or not api_key:
        parser.error("run needs --doubao-model, --base-url and EXPERT_WORK_EVAL_API_KEY")
    models = default_models(args.doubao_model)
    sessions = [
        s for s in plan(load_docs(args.docs), models, args.docs) if phase_of(s) == args.phase
    ]
    tracker = SpendTracker(stop_line=args.stop_line, prices={m.name: m.price for m in models})
    spent_file = args.out / "spent.json"
    if spent_file.exists():
        tracker.spent = float(json.loads(spent_file.read_text())["spent"])
    emit(
        f"approved quote {args.approved_quote}; stop line {args.stop_line}; "
        f"already spent {tracker.spent:.2f}"
    )
    try:
        asyncio.run(
            run_phase(
                sessions,
                docs_dir=args.docs,
                out=args.out,
                base_url=args.base_url,
                api_key=api_key,
                tracker=tracker,
                models=models,
                emit=emit,
            )
        )
    finally:
        args.out.mkdir(parents=True, exist_ok=True)
        spent_file.write_text(json.dumps({"spent": tracker.spent}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
