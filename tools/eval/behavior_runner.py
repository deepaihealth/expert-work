"""B-140 跑整套:每个用例的每次重复,在测试环境真跑,结果逐行写 JSONL。

用法(key 只经环境变量,不进命令行):
  EXPERT_WORK_API_TOKEN=… uv run --no-sync python tools/eval/behavior_runner.py --label base \\
      [--only g02-edit-three-places,g08-group-mean] [--repeats 3] [--concurrency 4] \\
      [--agent-map eval-general=eval-general-broken] [--note "test 5091938c"]

只允许测试环境或本机(规格 §8)。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import fnmatch
import os
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:  # 从仓库根目录以脚本方式运行
    sys.path.insert(0, str(_HERE))

from behavior_checks import FetchedFile, FileKey, evaluate, required_files  # noqa: E402
from behavior_client import ExternalClient, StreamIncompleteError  # noqa: E402
from behavior_schema import (  # noqa: E402
    FIXTURES_DIR,
    Case,
    CaseResult,
    ResultHeader,
    RunRecord,
    TurnRecord,
    append_line,
    case_set_hash,
    load_cases,
)

DEFAULT_BASE_URL = "https://expert-work-test.deepaihealth.com"
_TEST_HOSTS = frozenset({"expert-work-test.deepaihealth.com"})
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1"})
_ATTEMPTS = 3
_MAX_BACKOFF_S = 60.0


def _backoff_s(response: httpx.Response, attempt: int) -> float:
    """429 的等待秒数:优先 ``Retry-After``(上限 60 秒),否则 10 秒乘以第几次。"""
    try:
        return min(float(response.headers.get("Retry-After", "")), _MAX_BACKOFF_S)
    except ValueError:
        return min(10.0 * attempt, _MAX_BACKOFF_S)


class Client(Protocol):
    async def upload(
        self, agent: str, user_id: str, filename: str, data: bytes, session_id: str | None
    ) -> tuple[str, str]:
        """Upload one fixture; return ``(upload_id, session_id)``."""

    async def run_turn(
        self,
        agent: str,
        user_id: str,
        session_id: str | None,
        prompt: str,
        upload_ids: list[str],
        index: int,
    ) -> tuple[TurnRecord, str | None]:
        """Stream one turn; return its record and the session id."""

    async def download_artifact(
        self, agent: str, user_id: str, entry: dict[str, Any]
    ) -> bytes | None:
        """Download an artifact; ``None`` when it is gone."""

    async def read_workspace_file(self, agent: str, user_id: str, path: str) -> bytes | None:
        """Read a workspace file; ``None`` when it does not exist."""

    async def archive(self, agent: str, user_id: str, session_id: str) -> None:
        """Soft-archive the session."""


def check_base_url(url: str) -> None:
    """只放行测试环境(https)与本机 —— API key 会随每个请求发出去。"""
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if host in _LOCAL_HOSTS:
        return
    if host in _TEST_HOSTS and parsed.scheme == "https":
        return
    raise SystemExit(
        f"refusing to run against {url!r}: "
        f"B-140 runs only on https://{min(_TEST_HOSTS)} or localhost"
    )


def validate_agent_map(agent_map: dict[str, str], cases: list[Case]) -> None:
    """替换表的源必须是用例真用到的智能体 —— 写错时静默不换,对照会报一张假的「持平」。"""
    used = {c.agent for c in cases}
    unknown = sorted(set(agent_map) - used)
    if unknown:
        raise SystemExit(
            f"--agent-map source(s) {unknown} not used by the selected cases ({sorted(used)})"
        )


def find_artifact(turns: list[TurnRecord], pattern: str) -> dict[str, Any] | None:
    """最后一轮里最后登记的、名字匹配通配符的产物。"""
    for turn in reversed(turns):
        for entry in reversed(turn.artifacts):
            if entry.get("name") and fnmatch.fnmatchcase(str(entry["name"]), pattern):
                return entry
    return None


def metrics_of(record: RunRecord) -> dict[str, float]:
    out = {
        "tool_calls": float(sum(len(t.tool_calls) for t in record.turns)),
        "wall_s": round(sum(t.wall_s for t in record.turns), 1),
        "compactions": float(sum(t.compactions for t in record.turns)),
    }
    # 任何一轮没有用量就不出 token 指标 —— 半截求和比没有更误导。
    if record.turns and all(t.input_tokens is not None for t in record.turns):
        out["tokens_in"] = float(sum(t.input_tokens or 0 for t in record.turns))
    if record.turns and all(t.output_tokens is not None for t in record.turns):
        out["tokens_out"] = float(sum(t.output_tokens or 0 for t in record.turns))
    return out


def turn_metrics_of(record: RunRecord) -> list[dict[str, int | None]]:
    """逐轮的工具调用数、输入 token 与压缩次数 —— 带 ``turn`` 的阈值要按它校准(规格 §7)。"""
    return [
        {
            "tool_calls": len(t.tool_calls),
            "input_tokens": t.input_tokens,
            "compactions": t.compactions,
        }
        for t in record.turns
    ]


def indeterminate_reason(case: Case, record: RunRecord) -> str | None:
    """B-141 —— 要求压缩的用例整次一次都没压缩:判据测不到它要测的东西,不判过 / 不过。

    B-163 —— 要求工具失败的恢复用例同理:一次失败都没撞上就测不到恢复。
    """
    if case.requires_compaction and not any(t.compactions for t in record.turns):
        return "no compaction in any turn (requires_compaction)"
    if case.requires_tool_error and not any(t.tool_errors for t in record.turns):
        return "no tool error in any turn (requires_tool_error)"
    return None


async def run_case_once(
    client: Client,
    case: Case,
    *,
    rep: int,
    label: str,
    agent_map: dict[str, str],
    fixtures_dir: Path,
) -> tuple[RunRecord, dict[FileKey, FetchedFile | None]]:
    agent = agent_map.get(case.agent, case.agent)
    # 每次一个新 user_id:工作区按「用户 + 智能体」划分,复用会把上一次的 PLAN.md 带进来。
    user_id = f"b140-{label}-{case.id}-{rep}-{uuid.uuid4().hex[:6]}"
    session_id: str | None = None
    try:
        upload_ids: list[str] = []
        for name in case.fixtures:
            upload_id, session_id = await client.upload(
                agent, user_id, name, (fixtures_dir / name).read_bytes(), session_id
            )
            upload_ids.append(upload_id)
        turns: list[TurnRecord] = []
        for index, turn in enumerate(case.turns, start=1):
            turn_uploads = upload_ids if index == 1 else []
            for name in turn.fixtures:
                upload_id, session_id = await client.upload(
                    agent, user_id, name, (fixtures_dir / name).read_bytes(), session_id
                )
                turn_uploads = [*turn_uploads, upload_id]
            record, sid = await client.run_turn(
                agent, user_id, session_id, turn.prompt, turn_uploads, index
            )
            session_id = session_id or sid
            turns.append(record)
            if session_id is None and index < len(case.turns):
                # 下一轮会开新会话、丢掉上下文,多轮用例就测歪了。
                raise RuntimeError(f"no session id after turn {index}")
        files: dict[FileKey, FetchedFile | None] = {}
        for kind, name in required_files(case):
            if kind == "artifact":
                entry = find_artifact(turns, name)
                data = await client.download_artifact(agent, user_id, entry) if entry else None
                files[(kind, name)] = (
                    (str(entry["name"]), data) if entry and data is not None else None
                )
            else:
                data = await client.read_workspace_file(agent, user_id, name)
                files[(kind, name)] = (name, data) if data is not None else None
        return RunRecord(
            case_id=case.id, rep=rep, user_id=user_id, session_id=session_id, turns=turns
        ), files
    finally:
        if session_id is not None:
            with contextlib.suppress(httpx.HTTPError):
                await client.archive(agent, user_id, session_id)


async def run_one(
    client: Client,
    case: Case,
    rep: int,
    *,
    label: str,
    agent_map: dict[str, str],
    fixtures_dir: Path,
) -> CaseResult:
    last_error = ""
    for attempt in range(1, _ATTEMPTS + 1):
        try:
            record, files = await run_case_once(
                client, case, rep=rep, label=label, agent_map=agent_map, fixtures_dir=fixtures_dir
            )
        except (httpx.TransportError, StreamIncompleteError) as exc:
            last_error = f"attempt {attempt}: {type(exc).__name__}: {exc}"
            continue
        except httpx.HTTPStatusError as exc:
            last_error = (
                f"attempt {attempt}: HTTP {exc.response.status_code} {exc.request.url.path}"
            )
            if exc.response.status_code == 429:  # 配额 / 并发闸,等一等再来
                await asyncio.sleep(_backoff_s(exc.response, attempt))
                continue
            if exc.response.status_code < 500:
                break  # 其余 4xx 是用例或配置错,重试没用
            continue
        except Exception as exc:  # 评测框架自己的错:记下来、不判分,别让一条用例拖垮整批
            last_error = f"harness error: {type(exc).__name__}: {exc}"
            break
        verdicts = evaluate(case, record, files, fixtures_dir)
        indeterminate = indeterminate_reason(case, record)
        return CaseResult(
            case_id=case.id,
            rep=rep,
            passed=None if indeterminate else all(v.passed for v in verdicts),
            verdicts=verdicts,
            indeterminate=indeterminate,
            metrics=metrics_of(record),
            turn_metrics=turn_metrics_of(record),
            session_id=record.session_id,
        )
    return CaseResult(case_id=case.id, rep=rep, passed=None, infra_error=last_error)


async def run_suite(
    client: Client,
    cases: list[Case],
    *,
    header: ResultHeader,
    out_path: Path,
    repeats: int,
    concurrency: int,
    agent_map: dict[str, str],
    fixtures_dir: Path = FIXTURES_DIR,
) -> list[CaseResult]:
    append_line(out_path, header)
    gate = asyncio.Semaphore(concurrency)
    results: list[CaseResult] = []

    async def job(case: Case, rep: int) -> None:
        async with gate:
            res = await run_one(
                client,
                case,
                rep,
                label=header.label,
                agent_map=agent_map,
                fixtures_dir=fixtures_dir,
            )
        append_line(out_path, res)  # 单线程事件循环,两次 await 之间的同步写不会交错
        results.append(res)
        if res.indeterminate:
            mark = "INDET"
        else:
            mark = "INFRA" if res.passed is None else ("PASS" if res.passed else "FAIL")
        failed = [v.type for v in res.verdicts if not v.passed]
        reason = res.infra_error or res.indeterminate or ""
        print(f"{mark:5} {case.id} #{rep} {failed or ''} {reason}".rstrip(), flush=True)

    await asyncio.gather(*(job(c, r) for c in cases for r in range(1, repeats + 1)))
    return results


def _agent_map(items: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in items:
        src, sep, dst = item.partition("=")
        if not sep or not src or not dst:
            raise SystemExit(f"--agent-map wants FROM=TO, got {item!r}")
        out[src] = dst
    return out


async def _amain(args: argparse.Namespace, token: str, out: Path) -> int:
    cases = load_cases(only=args.only.split(",") if args.only else None)
    agent_map = _agent_map(args.agent_map)
    validate_agent_map(agent_map, cases)
    header = ResultHeader(
        label=args.label,
        started_at=datetime.now(UTC).isoformat(timespec="seconds"),
        base_url=args.base_url,
        note=args.note,
        agent_map=agent_map,
        case_set_hash=case_set_hash(),
        repeats=args.repeats,
        case_ids=[c.id for c in cases],
    )
    async with httpx.AsyncClient(
        base_url=args.base_url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=httpx.Timeout(30.0, read=600.0),
    ) as http:
        results = await run_suite(
            ExternalClient(http),
            cases,
            header=header,
            out_path=out,
            repeats=args.repeats,
            concurrency=args.concurrency,
            agent_map=agent_map,
        )
    passed = sum(r.passed is True for r in results)
    indeterminate = sum(r.indeterminate is not None for r in results)
    infra = sum(r.passed is None for r in results) - indeterminate
    print(
        f"done: {passed}/{len(results)} passed, {infra} infra errors, "
        f"{indeterminate} indeterminate -> {out}"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--label", required=True, help="本次运行的名字,如 base / cand-5091938c")
    parser.add_argument(
        "--base-url", default=os.environ.get("EXPERT_WORK_API_URL", DEFAULT_BASE_URL)
    )
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--only", default="", help="逗号分隔的用例 id")
    parser.add_argument(
        "--agent-map", action="append", default=[], help="FROM=TO,换用另一个评测智能体"
    )
    parser.add_argument("--note", default="", help="写进结果头,如测试环境镜像 tag")
    parser.add_argument("--out", default="", help="默认 eval-out/b140/<label>.jsonl")
    args = parser.parse_args(argv)
    check_base_url(args.base_url)
    token = os.environ.get("EXPERT_WORK_API_TOKEN")
    if not token:
        raise SystemExit(
            "set EXPERT_WORK_API_TOKEN (read it from the eval-credentials Secret into the env)"
        )
    out = Path(args.out or f"eval-out/b140/{args.label}.jsonl")
    if out.exists():
        raise SystemExit(f"{out} exists; pick a new --label or --out")
    return asyncio.run(_amain(args, token, out))


if __name__ == "__main__":
    raise SystemExit(main())
