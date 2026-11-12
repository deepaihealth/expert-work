"""B-302 PR 2 A/B —— 真跑那一段:对外平面的会话、止损、计费、交付件、清理(设计 §5.3、§5.5)。

``b302_ab.py`` 负责计划 / 报价 / 报告与命令行;这里只放会碰网络的部分,和它要用的帧解析。

**钱的规矩**(独立审查 HIGH-1 / HIGH-2):

* 每一轮(= 一次运行)开始前都判一次止损线,过线就不再开下一轮 —— 过线时在跑的最多是每家 2 路
  各 1 轮,所以实际花费 ≤ 止损线 + Σ 每家 2 x 每次运行上限(``b302_ab.hard_ceiling_yuan``)。
* 花费按 ``end`` 帧的 ``usage_by_model`` 累计(缓存读在顶层 ``cache_read_tokens``,见
  ``control_plane.api._run_usage.usage_buckets_to_wire``);没有 ``end`` 帧或没有用量时先查
  ``GET /v1/agents/{code}/runs/{run_id}/usage`` 补账,仍查不到就**阻塞**:不再派任何新运行,
  在跑的走完记账,原因写进记录与 ``spent.json``。没有单价的型号、某个会话抛异常、某一轮所有
  步都没有 ``usage_metadata``(25 万护栏失效)同样阻塞。
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import time
import zipfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import b302_metrics as metrics  # noqa: E402
from behavior_client import SseParser, message_text  # noqa: E402

#: 单次调用输入超过它就取消这次运行(§5.5 第 3 条)。
MAX_INPUT_TOKENS = 250_000
CONCURRENCY_PER_VENDOR = 2
#: 一轮的 HTTP 读超时:时限 90 分钟 + 收尾余量。
RUN_TIMEOUT_S = 7200.0
DELIVERABLE_EXT = {"slides": ".pptx", "spreadsheet": ".xlsx", "document": ".docx"}
AGENT_PREFIX = "eval-b302-"
_WRITERS = frozenset({"write_file", "edit_file", "exec_python", "bash", "save_artifact"})
_USER = "b302-ab"


@dataclass(frozen=True)
class Session:
    """一个会话:同一份文档在同一组里的几轮(每轮是一次运行)。"""

    phase: str  # pilot | main | regression
    model: str
    arm: str
    doc_id: str
    turns: int
    repeat: int = 0


_KEY = ("phase", "model", "arm", "doc_id", "repeat")


def session_fields(session: Session) -> dict[str, Any]:
    return asdict(session)


def record_key(record: Mapping[str, Any]) -> tuple[Any, ...]:
    return tuple(record.get(k, 0 if k == "repeat" else None) for k in _KEY)


def is_complete(record: Mapping[str, Any]) -> bool:
    """跑完了的会话(续跑时跳过):没出错、没被止损 / 阻塞拦下。"""
    return not record.get("error") and not record.get("stopped")


def dedupe(records: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """同一 (phase, model, arm, doc_id, repeat) 只留最后一条,按最后一条出现的次序。"""
    last = {record_key(r): i for i, r in enumerate(records)}
    return [r for i, r in enumerate(records) if last[record_key(r)] == i]


# --------------------------------------------------------------------------- spend


@dataclass
class SpendTracker:
    stop_line: float
    prices: Mapping[str, tuple[float, float, float]]
    spent: float = 0.0
    #: 计不了账时的原因;一旦有,就不再派任何新运行(``may_start`` 为假)。
    blocked: str | None = None

    def block(self, reason: str) -> None:
        if self.blocked is None:
            self.blocked = reason

    def add(self, usage_by_model: Iterable[Mapping[str, Any]] | None) -> float:
        """按对外形状累计一次运行的花费;``None`` = 不知道花了多少 → 阻塞。"""
        if usage_by_model is None:
            self.block("a run finished without usage; actual spend unknown")
            return 0.0
        cost = 0.0
        for bucket in usage_by_model:
            model = str(bucket.get("model"))
            if model not in self.prices:
                self.block(f"no price for model {model!r}; quote again before going on")
                continue
            inp, out, cache = self.prices[model]
            cached = int(bucket.get("cache_read_tokens") or 0)
            total_in = int(bucket.get("input_tokens") or 0)
            output = int(bucket.get("output_tokens") or 0)
            cost += ((total_in - cached) * inp + cached * cache + output * out) / 1e6
        self.spent += cost
        return cost

    def may_start(self) -> bool:
        return self.blocked is None and self.spent < self.stop_line

    def why_not(self) -> str:
        return self.blocked or f"stop line reached ({self.spent:.2f} >= {self.stop_line:.2f})"


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
                messages = node.get("messages") if isinstance(node, dict) else None
                for msg in messages or []:
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


def deliverable_candidates(steps: Sequence[metrics.Step], ext: str) -> list[str]:
    """写文件类调用参数里出现过的、以 ``ext`` 结尾的路径(最后写的在前),去掉 ``/workspace/``。"""
    pattern = re.compile(r"[\w./-]+" + re.escape(ext))
    found: list[str] = []
    for step in steps:
        for call in step.calls:
            if call.name in _WRITERS:
                found.extend(pattern.findall(json.dumps(call.args, ensure_ascii=False)))
    rels = [re.sub(r"^/?(workspace/)?", "", p) for p in reversed(found)]
    return list(dict.fromkeys(r for r in rels if r))


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


# --------------------------------------------------------------------------- one session (paid)


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


async def _usage_from_endpoint(http: Any, agent: str, run_id: str | None) -> list[Any] | None:
    """没接住 ``end`` 帧时的对账兜底(同一个装配口,逐字段同形);查不到 = ``None``。"""
    if run_id is None:
        return None
    resp = await http.get(f"/v1/agents/{agent}/runs/{run_id}/usage", params={"user_id": _USER})
    if resp.status_code != 200:
        return None
    usage = resp.json().get("usage_by_model")
    return usage if isinstance(usage, list) else None


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
    usage = end.get("usage_by_model") if collector.end is not None else None
    source = "end_frame"
    if usage is None:
        usage = await _usage_from_endpoint(http, agent, collector.run_id)
        source = "usage_endpoint" if usage is not None else "unknown"
    if usage is None:
        tracker.block(f"run {collector.run_id}: no end-frame usage and no usage endpoint record")
    else:
        tracker.add(usage)
    if collector.steps and not any(collector.input_tokens):
        tracker.block(
            f"run {collector.run_id}: no step carried usage_metadata input_tokens; the "
            f"{MAX_INPUT_TOKENS}-token guard cannot work"
        )
    return {
        "run_id": collector.run_id,
        "wall_s": round(time.monotonic() - started, 1),
        "steps": [asdict(s) for s in collector.steps],
        "input_tokens": collector.input_tokens,
        "warnings": collector.warnings,
        "tripped": collector.tripped,
        "exit_reason": end.get("exit_reason"),
        "status": end.get("status"),
        "usage_by_model": usage,
        "usage_source": source,
        "artifacts": end.get("artifacts") or [],
        "cancelled": collector.cancel_reason,
    }


async def _deliverable(
    http: Any, agent: str, session_id: str, record: Mapping[str, Any], ext: str
) -> tuple[bytes | None, str]:
    """``(字节, 来源)``:登记过的产物优先;没登记但写过这类文件的,按工作区路径去读
    (并计「未登记」)。"""
    entries = [a for a in record["artifacts"] if str(a.get("name", "")).endswith(ext)]
    if entries:
        entry = entries[-1]
        params = {
            "user_id": _USER,
            "name": entry["name"],
            **({"version": entry["version"]} if entry.get("version") is not None else {}),
        }
        resp = await http.get(f"/v1/agents/{agent}/artifacts/download", params=params)
        if resp.status_code == 200:
            return resp.content, "registered"
    for rel in deliverable_candidates(_steps_of(record), ext):
        for path in (f"threads/{session_id}/work/{rel}", rel):
            resp = await http.get(
                f"/v1/agents/{agent}/workspace/file", params={"user_id": _USER, "path": path}
            )
            if resp.status_code == 200:
                return resp.content, "workspace_unregistered"
    return None, "missing"


def _steps_of(turn: Mapping[str, Any]) -> tuple[metrics.Step, ...]:
    return tuple(
        metrics.Step(
            index=s["index"],
            thinking=s["thinking"],
            reply=s["reply"],
            calls=tuple(
                c if isinstance(c, metrics.Call) else metrics.Call(c["name"], c["args"])
                for c in s["calls"]
            ),
            reasoning_tokens=s.get("reasoning_tokens"),
        )
        for s in turn["steps"]
    )


async def run_session(
    http: Any,
    session: Session,
    *,
    doc: Mapping[str, Any],
    docs_dir: Path,
    agent: str,
    tracker: SpendTracker,
    quote_id: str | None = None,
) -> dict[str, Any]:
    """一个会话:第 1 轮看要点 → 第 2 轮做交付件(另存一份,准确率按它算)→ 第 3 轮用户更正。

    每一轮开始前都判止损线 / 阻塞;不过就停在这里,原因写进 ``stopped``(会话照样归档)。
    """
    pdf = (docs_dir / f"{session.doc_id}.pdf").read_bytes()
    upload_id, session_id = await _upload(http, agent, f"{session.doc_id}.pdf", pdf, None)
    ext = DELIVERABLE_EXT.get(str(doc.get("deliverable")), "")
    turns: list[dict[str, Any]] = []
    stopped: str | None = None
    try:
        for number in range(1, session.turns + 1):
            prompt = doc["prompts"].get(f"turn{number}")
            if prompt is None:
                break
            if not tracker.may_start():
                stopped = tracker.why_not()
                break
            uploads = [upload_id] if number == 1 else []
            if number == 2 and doc.get("template"):
                name = f"{session.doc_id}.template.md"
                tid, _ = await _upload(
                    http, agent, name, (docs_dir / name).read_bytes(), session_id
                )
                uploads.append(tid)
            record = await _turn(http, agent, session_id, str(prompt), uploads, tracker)
            record["turn"] = number
            if number >= 2 and ext:
                data, source = await _deliverable(http, agent, session_id, record, ext)
                record["deliverable_source"] = source
                record["deliverable_text"] = deliverable_text(data) if data else None
                record["deliverable_structure"] = (
                    deliverable_structure(data, str(doc["deliverable"])) if data else None
                )
            turns.append(record)
            if record.get("cancelled"):
                break
    finally:
        await http.delete(f"/v1/agents/{agent}/sessions/{session_id}", params={"user_id": _USER})
    return {**session_fields(session), "quote_id": quote_id, "turns": turns, "stopped": stopped}


# --------------------------------------------------------------------------- a phase (paid)


def read_records(out: Path) -> list[dict[str, Any]]:
    sink = out / "records.jsonl"
    if not sink.exists():
        return []
    return [json.loads(line) for line in sink.read_text(encoding="utf-8").splitlines() if line]


async def run_phase(
    sessions: Sequence[Session],
    *,
    docs: Mapping[str, Mapping[str, Any]],
    docs_dir: Path,
    out: Path,
    http: Any,
    tracker: SpendTracker,
    vendor: Mapping[str, str],
    agent_for: Callable[[Session], str],
    quote_id: str,
    emit: Callable[[str], None] = print,
) -> None:
    """按家并发 2 路;止损 / 阻塞后不再派新会话;已跑完的会话(``records.jsonl`` 里有)跳过。

    单个会话出错不取消别的:它的错误写一条记录并阻塞后续派发,在跑的会话走完、记账。
    """
    gates = {p: asyncio.Semaphore(CONCURRENCY_PER_VENDOR) for p in set(vendor.values())}
    out.mkdir(parents=True, exist_ok=True)
    sink = out / "records.jsonl"
    done = {record_key(r) for r in read_records(out) if is_complete(r)}

    def _write(record: Mapping[str, Any]) -> None:
        with sink.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    async def _one(session: Session) -> None:
        key = record_key(session_fields(session))
        if key in done:
            emit(f"already done, skipped {key}")
            return
        async with gates[vendor[session.model]]:
            if not tracker.may_start():
                emit(f"not started ({tracker.why_not()}): {key}")
                return
            try:
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
                    agent=agent_for(session),
                    tracker=tracker,
                    quote_id=quote_id,
                )
            except Exception as exc:
                tracker.block(f"session {key} failed: {exc!r}")
                _write({**session_fields(session), "quote_id": quote_id, "error": repr(exc)})
                emit(f"failed {key}: {exc!r}")
                return
            _write(record)
            emit(f"done {key} spent={tracker.spent:.2f} stopped={record['stopped']}")

    await asyncio.gather(*(_one(s) for s in sessions), return_exceptions=True)


# --------------------------------------------------------------------------- cleanup


async def cleanup(
    http: Any, *, yes: bool, emit: Callable[[str], None] = print
) -> list[tuple[str, str]]:
    """列出 ``eval-b302-`` 开头的临时智能体;``yes`` 才删(控制台平面,需要控制台登录凭据)。"""
    resp = await http.get("/v1/agents", params={"limit": 500})
    resp.raise_for_status()
    items = resp.json()["data"]["items"]
    targets = [
        (str(i["name"]), str(i["version"]))
        for i in items
        if str(i.get("name", "")).startswith(AGENT_PREFIX)
    ]
    for name, version in targets:
        if yes:
            deleted = await http.delete(f"/v1/agents/{name}/{version}")
            deleted.raise_for_status()
            emit(f"deleted {name}/{version}")
        else:
            emit(f"would delete {name}/{version} (pass --yes)")
    return targets
