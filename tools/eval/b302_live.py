"""B-302 PR 2 A/B —— 真跑那一段:对外平面的会话、止损、计费、交付件、清理(设计 §5.3、§5.5)。

``b302_ab.py`` 负责计划 / 报价 / 报告与命令行;这里只放会碰网络的部分,和它要用的帧解析。

**钱的规矩**(独立审查 HIGH-1 / HIGH-2):

* 每一轮(= 一次运行)开始前都判一次止损线,过线就不再开下一轮 —— 过线时在跑的最多是每家 2 路
  各 1 轮,所以实际花费 ≤ 止损线 + Σ 每家 2 x 每次运行上限(``b302_ab.hard_ceiling_yuan``)。
* 花费按 ``end`` 帧的 ``usage_by_model`` 累计(缓存读在顶层 ``cache_read_tokens``,见
  ``control_plane.api._run_usage.usage_buckets_to_wire``);没有 ``end`` 帧或没有用量时先查
  ``GET /v1/agents/{code}/runs/{run_id}/usage`` 补账:它说 run 还在跑就先取消、轮询到终态再记
  (复审 R1);流中途断了同样先取消再补账(R3)。仍查不到、或等不到终态,就按这个型号一次运行的
  上限计入并**阻塞**:不再派任何新运行,在跑的走完记账,原因写进记录与 ``spent.json``。没有单价
  的型号、某个会话抛异常、某一轮所有步都没有 ``usage_metadata``(25 万护栏失效)同样阻塞。
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
#: 补账接口说 run 还没结束时:先取消,再每 ``SETTLE_POLL_S`` 秒问一次,最多等 ``SETTLE_TIMEOUT_S`` 秒。
SETTLE_POLL_S = 5.0
SETTLE_TIMEOUT_S = 180.0
#: ``GET .../runs/{id}/usage`` 的 ``run_status`` 里还会变的那几个(``ExternalRunStatus``);
#: 其余(success / error / timeout / interrupted / paused / incomplete)都是这次运行不会再花钱了。
LIVE_RUN_STATUSES = frozenset({"pending", "queued", "running"})
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
    #: 每个型号一次运行的花费上限(元,§5.5);算不清一次运行花了多少时按它计。
    run_caps: Mapping[str, float] = field(default_factory=dict)

    def block(self, reason: str) -> None:
        if self.blocked is None:
            self.blocked = reason

    def charge_cap(self, model: str, reason: str) -> float:
        """这次运行花了多少说不清:按这个型号一次运行的上限计入,并阻塞。没有上限就取最大的。"""
        cap = self.run_caps.get(model, max(self.run_caps.values(), default=0.0))
        self.spent += cap
        self.block(reason)
        return cap

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


#: 带路径参数的写文件类工具 → 参数名(``save_artifact`` 不给 path 时就是 name)。
_PATH_ARGS = {"write_file": ("path",), "edit_file": ("path",), "save_artifact": ("path", "name")}


def deliverable_candidates(steps: Sequence[metrics.Step], ext: str) -> list[str]:
    """写过的、以 ``ext`` 结尾的工作区路径,最可信的在前:先是 ``write_file`` / ``save_artifact``
    等的路径参数(最后写的在前,路径里可以有空格),再是 bash / exec 代码里引号括起来或不带空格的
    路径。去掉 ``/workspace/`` 前缀;含 ``..`` 的一律丢掉(不去读工作区以外的东西)。"""
    quoted = re.compile(r"[\"']([^\"'\n]+" + re.escape(ext) + r")[\"']")
    bare = re.compile(r"[\w./-]+" + re.escape(ext))
    direct: list[str] = []
    scraped: list[str] = []
    for step in steps:
        for call in step.calls:
            if call.name in _PATH_ARGS:
                for key in _PATH_ARGS[call.name]:
                    value = call.args.get(key)
                    if isinstance(value, str) and value.endswith(ext):
                        direct.append(value)
                        break
            elif call.name in _WRITERS:
                text = " ".join(str(v) for v in call.args.values())
                scraped.extend(quoted.findall(text))
                scraped.extend(bare.findall(quoted.sub(" ", text)))
    ordered = [*reversed(direct), *reversed(scraped)]
    rels = [re.sub(r"^/?(workspace/)?", "", p.strip()) for p in ordered]
    return list(dict.fromkeys(r for r in rels if r and ".." not in r.split("/")))


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


async def _usage_from_endpoint(
    http: Any, agent: str, run_id: str
) -> tuple[str | None, list[Any] | None]:
    """``GET .../runs/{id}/usage``(与 ``end`` 帧同一个装配口):``(run_status, usage_by_model)``。
    ``run_status`` 是非终态时用量只是「到目前为止」的,调用方自己判(``external_runs.run_usage``)。"""
    resp = await http.get(f"/v1/agents/{agent}/runs/{run_id}/usage", params={"user_id": _USER})
    if resp.status_code != 200:
        return None, None
    body = resp.json()
    usage = body.get("usage_by_model")
    return body.get("run_status"), usage if isinstance(usage, list) else None


async def _cancel(http: Any, agent: str, run_id: str) -> None:
    await http.post(f"/v1/agents/{agent}/runs/{run_id}:cancel", json={"user_id": _USER})


async def _settle(
    http: Any,
    agent: str,
    run_id: str | None,
    *,
    model: str,
    tracker: SpendTracker,
    why: str = "",
) -> tuple[list[Any] | None, str]:
    """没接住 ``end`` 帧时给这次运行记账,返回 ``(用量, 来源)``。

    run 还在跑(``LIVE_RUN_STATUSES``)→ 先取消,再短轮询到终态,按终态的用量记;等不到终态 →
    按这个型号一次运行的上限计入并阻塞。终态但没有用量记录 → 阻塞。连 run id 都没有(运行还没
    开始就断了,或开始了但没收到第一帧)→ 同样按上限计入并阻塞,宁可多算。"""
    if run_id is None:
        tracker.charge_cap(
            model, f"a run broke before its run id was known; charged at the cap {why}".strip()
        )
        return None, "cap_charged"
    status, usage = await _usage_from_endpoint(http, agent, run_id)
    if status in LIVE_RUN_STATUSES:
        await _cancel(http, agent, run_id)
        deadline = time.monotonic() + SETTLE_TIMEOUT_S
        while status in LIVE_RUN_STATUSES and time.monotonic() < deadline:
            await asyncio.sleep(SETTLE_POLL_S)
            status, usage = await _usage_from_endpoint(http, agent, run_id)
        if status in LIVE_RUN_STATUSES:
            tracker.charge_cap(
                model,
                f"run {run_id} did not settle after cancel (last status {status}); "
                "charged at the cap",
            )
            return usage, "cap_charged"
    if usage is None:
        tracker.charge_cap(
            model,
            f"run {run_id}: no end-frame usage and no usage endpoint record; charged at the cap",
        )
        return None, "cap_charged"
    tracker.add(usage)
    return usage, "usage_endpoint"


async def _turn(
    http: Any,
    agent: str,
    session_id: str,
    prompt: str,
    uploads: list[str],
    tracker: SpendTracker,
    *,
    model: str,
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
    try:
        async with http.stream("POST", f"/v1/agents/{agent}/runs", json=payload) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                frame = parser.feed(line)
                if frame is not None:
                    collector.on_frame(*frame)
                if collector.cancel_reason and not cancelled and collector.run_id:
                    cancelled = True
                    await _cancel(http, agent, collector.run_id)
    except BaseException as exc:
        # 复审 R3 —— 流中途断了:这次运行可能还在花钱。取消它、按补账接口记账(查不到按上限),
        # 再把异常照常抛出去(由 ``run_phase`` 阻塞后续派发)。
        if collector.run_id is not None and not cancelled:
            await _cancel(http, agent, collector.run_id)
        await _settle(http, agent, collector.run_id, model=model, tracker=tracker, why=repr(exc))
        raise
    tail = parser.flush()
    if tail is not None:
        collector.on_frame(*tail)
    end = collector.end or {}
    usage = end.get("usage_by_model") if collector.end is not None else None
    source = "end_frame"
    if usage is None:
        usage, source = await _settle(http, agent, collector.run_id, model=model, tracker=tracker)
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


def _parse_deliverable(data: bytes | None, source: str, deliverable: str) -> dict[str, Any]:
    """复审 R2 —— 交付件读不了(不是 zip、缺部件……)按「没有交付件」算分,不往上抛:这一轮已经
    付过钱,它的轨迹要留下;来源记成 ``unreadable``,``report`` 单独计数。"""
    if data is None:
        return {
            "deliverable_source": source,
            "deliverable_text": None,
            "deliverable_structure": None,
        }
    try:
        return {
            "deliverable_source": source,
            "deliverable_text": deliverable_text(data),
            "deliverable_structure": deliverable_structure(data, deliverable),
        }
    except (zipfile.BadZipFile, KeyError, ValueError, OSError, UnicodeError) as exc:
        return {
            "deliverable_source": "unreadable",
            "deliverable_error": repr(exc),
            "deliverable_text": None,
            "deliverable_structure": None,
        }


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
            record = await _turn(
                http, agent, session_id, str(prompt), uploads, tracker, model=session.model
            )
            record["turn"] = number
            if number >= 2 and ext:
                data, source = await _deliverable(http, agent, session_id, record, ext)
                record.update(_parse_deliverable(data, source, str(doc["deliverable"])))
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
    http: Any,
    *,
    codes: Iterable[str],
    yes: bool,
    emit: Callable[[str], None] = print,
    page: int = 500,
) -> list[tuple[str, str]]:
    """只删 ``codes``(``b302_ab.write_manifests`` 会生成的确切 code)里的临时智能体;``yes`` 才删。

    按 ``offset`` 翻页列全;打印每个匹配项的租户与总数。走控制台平面(需要控制台登录凭据)。"""
    wanted = set(codes)
    targets: list[tuple[str, str, str]] = []
    offset = 0
    while True:
        resp = await http.get("/v1/agents", params={"limit": page, "offset": offset})
        resp.raise_for_status()
        items = resp.json()["data"]["items"]
        targets.extend(
            (str(i["name"]), str(i["version"]), str(i.get("tenant_id")))
            for i in items
            if str(i.get("name")) in wanted
        )
        if len(items) < page:
            break
        offset += page
    emit(f"{len(targets)} matching temporary agents")
    for name, version, tenant in targets:
        if yes:
            deleted = await http.delete(f"/v1/agents/{name}/{version}")
            deleted.raise_for_status()
            emit(f"deleted {name}/{version} (tenant {tenant})")
        else:
            emit(f"would delete {name}/{version} (tenant {tenant}); pass --yes")
    return [(name, version) for name, version, _tenant in targets]
