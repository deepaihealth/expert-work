"""ClinConsensus 健康问答小测:裸模型 vs 平台智能体,同一提示词、同一判分。

数据:ClinConsensus 公开的 900 例低难度档(阿里,CC BY 4.0,不进仓库,``download`` 现拉)。
判分:每题 30 条判分项逐条判,提示词逐字取自官方 ``docs/judge_prompt.md``;
计分与官方 ``scripts/eval_cacs.py`` 同一公式(判分项通过率 / Pass@10 / CACS@10)。

只在本机手动跑,CI 只测确定性部分(``test_clinconsensus.py``)。用法(仓库根目录)::

    uv run --no-sync python tools/eval/clinconsensus.py download
    uv run --no-sync python tools/eval/clinconsensus.py select --n 10
    EXPERT_WORK_EVAL_LLM_API_KEY=... \\
    EXPERT_WORK_EVAL_LLM_BASE_URL=https://open.bigmodel.cn/api/paas/v4 \\
        uv run --no-sync python tools/eval/clinconsensus.py bare
    EXPERT_WORK_API_TOKEN=... uv run --no-sync python tools/eval/clinconsensus.py platform
    EXPERT_WORK_EVAL_JUDGE_API_KEY=... EXPERT_WORK_EVAL_JUDGE_BASE_URL=https://api.deepseek.com \\
        uv run --no-sync python tools/eval/clinconsensus.py judge
    uv run --no-sync python tools/eval/clinconsensus.py score

平台组先在控制台导入 ``datasets/clinconsensus/eval-clin.yaml``
(提示词与 ``SYSTEM_PROMPT`` 逐字相同)。
每一步可断点续跑:已有结果的题跳过。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:  # 从仓库根目录以脚本方式运行
    sys.path.insert(0, str(_HERE))

DATA_URL = "https://huggingface.co/datasets/skylenage/ClinConsensus/resolve/main/data/clinconsensus_low.jsonl"
DEFAULT_OUT = Path("eval-out/clinconsensus")
SYSTEM_PROMPT = (
    "你是一名专业、严谨的医疗健康咨询助手。请根据用户提供的信息,给出准确、全面、可操作的回答;"
    "涉及诊断、用药或治疗时说明依据和注意事项,必要时建议就医。不要编造信息。\n"
)
ANSWER_MODEL = "glm-5.3"
JUDGE_MODEL = "deepseek-v4-pro"
PLATFORM_AGENT = "eval-clin"
DEFAULT_SEED = 20261009
RUBRICS_PER_CASE = 30
THRESHOLD = 10
#: 用户角色里出现这些字样算「面向专业人员」,其余算「面向普通人」(与论文表 13 的切法同向)。
PRO_MARKERS = ("医生", "医师", "护士", "医学", "规培", "实习", "麻醉", "带教", "校医", "医务")
#: 元 / 百万 token:(未命中输入, 缓存命中输入, 输出)。glm-5.3 价目表口径。
GLM_PRICE = (8.0, 2.0, 28.0)

JUDGE_PROMPT = """Your job is to evaluate whether the assistant's response meets the criteria of a specific rubric item.

# User Query
{user_message}

# Assistant Response
{response}

# Rubric Item to Evaluate
{rubric_item}

# Instructions
Evaluate whether the assistant's response meets the criteria described in the rubric item above.

Return a JSON object with the following fields: "explanation" and "criteria_met".
- "explanation": A concise explanation of why the response does or does not meet the criteria.
- "criteria_met": A boolean (true/false) indicating whether the response meets the criteria.

Important rules:
1. If a rubric item has multiple criteria or sentences, ALL must be met for criteria_met to be true. If any single criterion is not met, return false.
2. If a criterion uses cue words such as "such as", "for example", or "including", the response does not need to include every listed example; it only needs to address the general requirement adequately.
3. Judge factual medical accuracy. If the rubric specifies a drug, dosage, guideline, or medical fact, the response must be consistent with current medical knowledge.
4. If the rubric asks whether a condition is satisfied, evaluate strictly: the response must clearly demonstrate the required behavior."""  # noqa: E501


# --- 纯函数(CI 覆盖) ---------------------------------------------------------


def user_message(case: dict[str, Any]) -> str:
    context = (case.get("clinical_context") or "").strip()
    request = (case.get("user_request") or "").strip()
    return f"{context}\n\n{request}" if context else request


def is_professional(case: dict[str, Any]) -> bool:
    return any(marker in case["user_role"] for marker in PRO_MARKERS)


def select_cases(rows: list[dict[str, Any]], n: int, seed: int) -> list[dict[str, Any]]:
    """普通人 / 专业人员各取一半(n 为奇数时普通人多一题),固定种子可复现。"""
    pro = [r for r in rows if is_professional(r)]
    lay = [r for r in rows if not is_professional(r)]
    rng = random.Random(seed)  # noqa: S311 — 抽样复现用,不涉及安全
    return rng.sample(lay, n - n // 2) + rng.sample(pro, n // 2)


def cacs(met: int, rubric_count: int, threshold: int) -> float:
    """官方 eval_cacs.py 的单例 CACS@k:阈值到满分之间,逐档是否达到的平均(百分数)。"""
    if threshold > rubric_count:
        return 0.0
    levels = range(threshold, rubric_count + 1)
    return 100.0 * sum(met >= level for level in levels) / len(levels)


def glm_cost(prompt: int, cached: int, completion: int) -> float:
    miss, hit, out = GLM_PRICE
    return ((prompt - cached) * miss + cached * hit + completion * out) / 1e6


def summarize(judged: list[dict[str, Any]], cases: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """一组的判分结果 → 判分项通过率 / Pass@10 / CACS@10,另按普通人 / 专业人员拆开。"""
    per_case = []
    for row in judged:
        met = sum(1 for j in row["judgments"] if j["criteria_met"])
        per_case.append(
            {
                "case_id": row["case_id"],
                "professional": is_professional(cases[row["case_id"]]),
                "met": met,
                "judge_errors": sum(1 for j in row["judgments"] if j["criteria_met"] is None),
                "cacs_at_10": round(cacs(met, RUBRICS_PER_CASE, THRESHOLD), 1),
            }
        )

    def agg(items: list[dict[str, Any]]) -> dict[str, Any]:
        n = len(items)
        if n == 0:
            return {"cases": 0}
        return {
            "cases": n,
            "rubric_accuracy_pct": round(
                100.0 * sum(p["met"] for p in items) / (RUBRICS_PER_CASE * n), 1
            ),
            "pass_at_10_pct": round(100.0 * sum(p["met"] >= THRESHOLD for p in items) / n, 1),
            "cacs_at_10_pct": round(sum(p["cacs_at_10"] for p in items) / n, 1),
            "judge_errors": sum(p["judge_errors"] for p in items),
        }

    return {
        "all": agg(per_case),
        "lay": agg([p for p in per_case if not p["professional"]]),
        "professional": agg([p for p in per_case if p["professional"]]),
        "per_case": per_case,
    }


# --- 文件 --------------------------------------------------------------------


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    with path.open("a") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def done_ids(path: Path) -> set[str]:
    return {r["case_id"] for r in read_jsonl(path)} if path.exists() else set()


def require_env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise SystemExit(f"missing env {name}")
    return value


# --- 步骤 --------------------------------------------------------------------


async def cmd_download(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as http:
        resp = await http.get(DATA_URL)
        resp.raise_for_status()
    (out / "clinconsensus_low.jsonl").write_bytes(resp.content)
    print("saved", len(resp.content), "bytes")


def cmd_select(out: Path, n: int, seed: int) -> None:
    rows = read_jsonl(out / "clinconsensus_low.jsonl")
    picked = select_cases(rows, n, seed)
    (out / "cases.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in picked)
    )
    for r in picked:
        print(r["case_id"], r["user_role"], "|", r["subject"])


async def cmd_bare(out: Path) -> None:
    key = require_env("EXPERT_WORK_EVAL_LLM_API_KEY")
    base = require_env("EXPERT_WORK_EVAL_LLM_BASE_URL")
    dest = out / "bare.jsonl"
    skip = done_ids(dest)
    async with httpx.AsyncClient(base_url=base, timeout=900) as http:
        for case in read_jsonl(out / "cases.jsonl"):
            if case["case_id"] in skip:
                continue
            started = time.monotonic()
            resp = await http.post(
                "/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json={
                    "model": ANSWER_MODEL,
                    "temperature": 0.2,
                    "max_tokens": 32768,
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": user_message(case)},
                    ],
                },
            )
            resp.raise_for_status()
            body = resp.json()
            usage = body.get("usage") or {}
            cached = int((usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
            prompt = int(usage.get("prompt_tokens") or 0)
            completion = int(usage.get("completion_tokens") or 0)
            row = {
                "case_id": case["case_id"],
                "response": body["choices"][0]["message"].get("content") or "",
                "prompt_tokens": prompt,
                "cached_tokens": cached,
                "completion_tokens": completion,
                "cost_yuan": round(glm_cost(prompt, cached, completion), 4),
                "wall_s": round(time.monotonic() - started, 1),
            }
            append_jsonl(dest, row)
            print(
                row["case_id"], completion, "out tokens", row["cost_yuan"], "元", row["wall_s"], "s"
            )


async def cmd_platform(out: Path, base_url: str) -> None:
    from behavior_client import ExternalClient
    from behavior_runner import check_base_url

    check_base_url(base_url)
    token = require_env("EXPERT_WORK_API_TOKEN")
    dest = out / "platform.jsonl"
    skip = done_ids(dest)
    async with httpx.AsyncClient(
        base_url=base_url, headers={"Authorization": f"Bearer {token}"}, timeout=1800
    ) as http:
        client = ExternalClient(http)
        for case in read_jsonl(out / "cases.jsonl"):
            if case["case_id"] in skip:
                continue
            user_id = f"clin-{uuid.uuid4().hex[:8]}"
            turn, session_id = await client.run_turn(
                PLATFORM_AGENT, user_id, None, user_message(case), [], 0
            )
            row = {
                "case_id": case["case_id"],
                "response": turn.final_text,
                "status": turn.status,
                "input_tokens": turn.input_tokens,
                "output_tokens": turn.output_tokens,
                "tool_calls": [c.name for c in turn.tool_calls],
                "run_id": turn.run_id,
                "session_id": session_id,
                "wall_s": turn.wall_s,
            }
            append_jsonl(dest, row)
            print(
                row["case_id"],
                row["status"],
                row["output_tokens"],
                "out",
                row["wall_s"],
                "s",
                row["tool_calls"],
            )


async def judge_one(
    http: httpx.AsyncClient,
    key: str,
    sem: asyncio.Semaphore,
    query: str,
    response: str,
    rubric: str,
) -> dict[str, Any]:
    prompt = JUDGE_PROMPT.format(user_message=query, response=response, rubric_item=rubric)
    async with sem:
        for attempt in range(3):
            try:
                resp = await http.post(
                    "/chat/completions",
                    headers={"Authorization": f"Bearer {key}"},
                    json={
                        "model": JUDGE_MODEL,
                        "temperature": 0,
                        "thinking": {"type": "disabled"},
                        "response_format": {"type": "json_object"},
                        "messages": [{"role": "user", "content": prompt}],
                    },
                )
                resp.raise_for_status()
                body = resp.json()
                verdict = json.loads(body["choices"][0]["message"]["content"])
                return {
                    "criteria_met": bool(verdict.get("criteria_met")),
                    "explanation": verdict.get("explanation", ""),
                    "usage": body.get("usage") or {},
                }
            except (httpx.HTTPError, json.JSONDecodeError, KeyError) as exc:
                if attempt == 2:  # 判不出记 None,计分时单列「判分出错」,不当作没做到
                    return {
                        "criteria_met": None,
                        "explanation": f"judge_error: {exc!r}",
                        "usage": {},
                    }
                await asyncio.sleep(2 * (attempt + 1))
    raise AssertionError("unreachable")


async def cmd_judge(out: Path) -> None:
    key = require_env("EXPERT_WORK_EVAL_JUDGE_API_KEY")
    base = require_env("EXPERT_WORK_EVAL_JUDGE_BASE_URL")
    cases = {c["case_id"]: c for c in read_jsonl(out / "cases.jsonl")}
    sem = asyncio.Semaphore(8)
    async with httpx.AsyncClient(base_url=base, timeout=300) as http:
        for arm in ("bare", "platform"):
            src, dest = out / f"{arm}.jsonl", out / f"judge_{arm}.jsonl"
            if not src.exists():
                print(f"skip {arm}: no answers yet")
                continue
            skip = done_ids(dest)
            for ans in read_jsonl(src):
                if ans["case_id"] in skip:
                    continue
                case = cases[ans["case_id"]]
                verdicts = await asyncio.gather(
                    *(
                        judge_one(
                            http, key, sem, user_message(case), ans["response"], r["criterion"]
                        )
                        for r in case["rubrics"]
                    )
                )
                usage = [v.pop("usage") for v in verdicts]
                row = {
                    "case_id": ans["case_id"],
                    "judgments": [
                        {"criterion_id": r["criterion_id"], **v}
                        for r, v in zip(case["rubrics"], verdicts, strict=True)
                    ],
                    "judge_prompt_tokens": sum(int(u.get("prompt_tokens") or 0) for u in usage),
                    "judge_completion_tokens": sum(
                        int(u.get("completion_tokens") or 0) for u in usage
                    ),
                }
                append_jsonl(dest, row)
                met = sum(1 for j in row["judgments"] if j["criteria_met"])
                print(arm, row["case_id"], f"{met}/{RUBRICS_PER_CASE}")


def cmd_score(out: Path) -> None:
    cases = {c["case_id"]: c for c in read_jsonl(out / "cases.jsonl")}
    report = {
        arm: summarize(read_jsonl(out / f"judge_{arm}.jsonl"), cases)
        for arm in ("bare", "platform")
        if (out / f"judge_{arm}.jsonl").exists()
    }
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1))
    print(
        json.dumps(
            {arm: {k: v for k, v in r.items() if k != "per_case"} for arm, r in report.items()},
            ensure_ascii=False,
            indent=1,
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "step", choices=["download", "select", "bare", "platform", "judge", "score"]
    )
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--n", type=int, default=10)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument(
        "--base-url",
        default=os.environ.get("EXPERT_WORK_API_URL", "https://expert-work-test.deepaihealth.com"),
    )
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    if args.step == "download":
        asyncio.run(cmd_download(args.out))
    elif args.step == "select":
        cmd_select(args.out, args.n, args.seed)
    elif args.step == "bare":
        asyncio.run(cmd_bare(args.out))
    elif args.step == "platform":
        asyncio.run(cmd_platform(args.out, args.base_url))
    elif args.step == "judge":
        asyncio.run(cmd_judge(args.out))
    else:
        cmd_score(args.out)


if __name__ == "__main__":
    main()
