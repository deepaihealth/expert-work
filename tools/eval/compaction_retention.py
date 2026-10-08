"""B-165 压缩摘要保留率离线评测(调研报告「第 1 层」)—— 不起智能体,直接调压缩器的摘要路径。

**测什么**:同一份合成长历史,交给摘要模型之前怎么拼(A 组 = B-141 之前的拼法,B 组 = 当前代码),
摘要里还剩几根「针」。针是不会自然出现的唯一编号,按中段位置 5%~95% 均匀摆 12 根(35%~75% 这段
5 根,正是旧版「头 2/3 + 尾 1/3」整块丢掉的区域):用户原话 5 根、助手正文 3 根、读文件结果的前段
2 根、**只出现在 write_file 参数里** 2 根(B-165 之前摘要模型看不到参数)。中段里还有一条进度线
(阶段 STAGE-01…STAGE-08 依次完成,最后完成的在约 85% 处,紧接着写明下一阶段),检查摘要是否
写对「做到第几阶段、下一阶段是什么」。

**两份夹具**:F1 按当前拼法截完约 14 万字符(在 16 万预算内,不触发收起);F2 约 30 万字符(强制
走「从最旧的工具结果、再到工具参数收起」的路径)。夹具全部确定性生成,内容是通用办公任务的形态,
不含任何租户业务。

**两组**:
- A 组:B-141 之前的 ``_format_middle_for_summary``(每条 2,000 字符、总 24,000、头 2/3 + 尾 1/3),
  原样拷在本文件 :func:`format_middle_pre_b141`,钉在提交 ``880b5a46``。提示词用当前的,
  所以两组只差在输入怎么拼。
- B 组:pod 里装的当前代码,走 ``ContextCompressor._compress_once``(真实的切分 + 拼法 + 提示词)。
  运行时打印 B 组是 B-165(看得到工具参数)还是 B-141(看不到)。

**模型**:用 ``build_step_routers`` 建出的 ``compression`` 路由 —— 与生产同一取法(B-143 默认主模型
同一家的便宜型号、备用链挂主模型),凭据从平台凭据表经 secret store 解出,和 run 走的是同一条路。
默认主模型 ``glm/glm-5.3`` → 摘要模型 ``glm-5.3-flash``。

**判分**:摘要里逐字查编号(不用 LLM 当评委);按位置分桶(early <35% / mid 35%~75% / late >75%)
与按角色统计;进度线判对 = 摘要里同时有最后完成的 ``STAGE-08`` 与下一阶段 ``STAGE-09``。同时打印
每组输入里有几根针(第 0 层覆盖面,零成本)与输入 / 输出 token 合计。

**判据**(调研报告 §6(b),先写死再跑):B 组 mid 桶保留率比 A 组高 ≥ 30 个百分点且各次稳定;B 组
mid 桶不低于两端(early、late)的 70%;进度线每次都对。合成针字面显眼,结果是**上限**,真实场景只会
更差;离线评测量不到「压缩后智能体多走了几步」。

**成本**(flash 输入 0.8 元 / 输出 2.8 元每百万 token,约 1.3 字符 / token):``--repeats 3`` 共
12 次调用,约 0.8 元;串行约 10~25 分钟。

**怎么跑**(测试集群;脚本自包含,stdin 喂进 control-plane pod 里的 python。B 组测的是 pod 镜像里的
代码,所以先确认 pod 跑的是要测的镜像 tag):

    export KUBECONFIG=~/.kube/expert-work-test.yaml
    POD=$(kubectl -n expert-work get pod -l app.kubernetes.io/name=control-plane \\
          --field-selector=status.phase=Running -o name | head -1)
    kubectl -n expert-work exec -i "$POD" -- python - --repeats 3 \\
        < tools/eval/compaction_retention.py

只打印夹具尺寸与第 0 层覆盖面、不调模型(本地也能跑)::

    uv run --no-sync python tools/eval/compaction_retention.py --dry-run

可选参数:``--fixtures F1,F2``、``--arms A,B``、``--provider`` / ``--model``(主模型)、
``--tenant-id``(按该租户的有效凭据解析,默认只看平台凭据)。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage

# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------

#: 12 根针的角色,按位置从前到后。35%~75% 那 5 根:args / assistant / user / tool / user。
NEEDLE_KINDS: tuple[str, ...] = (
    "user",
    "assistant",
    "tool",
    "user",
    "args",
    "assistant",
    "user",
    "tool",
    "user",
    "args",
    "assistant",
    "user",
)
NEEDLE_FIRST, NEEDLE_LAST = 0.05, 0.95
#: 进度线:阶段 1..STAGES_DONE 依次完成,最后一个在约 85% 处;下一阶段 = STAGES_DONE + 1。
STAGES_DONE = 8
STAGE_FIRST, STAGE_LAST = 0.15, 0.85
HEAD_KEEP, TAIL_KEEP = 4, 6

#: 夹具名 → 中段步数。每步按当前拼法约 2,650 字符;单测钉住两份的实际尺寸。
FIXTURES: dict[str, int] = {"F1": 53, "F2": 113}

_FILLER = (
    "本段为合成的工作记录,只用于撑出上下文长度:核对字段名称、统一日期格式、"
    "去掉重复的行、把金额保留两位小数、按月份分组汇总,然后再检查一遍合计是否一致。"
)
_CODE_PREFIXES = ("QX", "LM", "TR", "VB", "KD", "PN", "WJ", "HZ", "CY", "RF", "GS", "MT")


@dataclass(frozen=True)
class Needle:
    code: str
    kind: str
    position: float  # 在中段里的相对位置(0~1)
    step: int


@dataclass(frozen=True)
class Fixture:
    name: str
    messages: list[BaseMessage]
    needles: list[Needle]
    last_stage: str
    next_stage: str


def needle_code(k: int) -> str:
    return f"{_CODE_PREFIXES[k]}-{7391 + 113 * k}"


def stage_code(j: int) -> str:
    return f"STAGE-{j:02d}"


def _spread(first: float, last: float, count: int, steps: int) -> list[int]:
    return [round((first + (last - first) * k / (count - 1)) * (steps - 1)) for k in range(count)]


def _filler(chars: int) -> str:
    return (_FILLER * (chars // len(_FILLER) + 1))[:chars]


def _step_messages(i: int, needle: Needle | None, stage: int | None) -> list[BaseMessage]:
    """一步 = 用户要求 + 助手说明与一次工具调用 + 工具结果。针与进度句按需插进对应角色。"""
    kind = needle.kind if needle else None
    code = needle.code if needle else ""
    # 带针的 args 步一定是写文件、tool 步一定是读文件;其余按奇偶交替。
    write = kind == "args" or (kind != "tool" and i % 2 == 1)
    user = f"第 {i} 项:请处理 data_{i:03d}.csv。" + (
        f"顺便记一下,这批的批次号是 {code}。" if kind == "user" else ""
    )
    prose = f"好的,开始处理 data_{i:03d}.csv。" + (
        f"我记下对照编号 {code}。" if kind == "assistant" else ""
    )
    if stage is not None:
        prose += f"进度:阶段 {stage_code(stage)} 已完成。"
        if stage == STAGES_DONE:
            prose += f"下一步:阶段 {stage_code(stage + 1)}。"
    call_id = f"call-{i:03d}"
    if write:
        content = (f"# 汇总\n编号 {code}\n" if kind == "args" else "# 汇总\n") + _filler(2_500)
        call = {"path": f"out/summary_{i:03d}.md", "content": content}
        result = f"已写入 out/summary_{i:03d}.md,共 {len(content)} 字符。"
    else:
        call = {"path": f"data/data_{i:03d}.csv"}
        result = (
            f"读取 data/data_{i:03d}.csv 成功。"
            + (f"文件头校验码:{code}。" if kind == "tool" else "")
            + _filler(2_500)
        )
    name = "write_file" if write else "read_file"
    return [
        HumanMessage(content=user + _filler(600)),
        AIMessage(
            content=prose + _filler(400),
            tool_calls=[{"name": name, "args": call, "id": call_id, "type": "tool_call"}],
        ),
        ToolMessage(content=result, tool_call_id=call_id, name=name),
    ]


def build_fixture(name: str, steps: int) -> Fixture:
    """确定性夹具:开头 4 条 + 中段 ``steps`` 步 + 末尾 6 条(两步,不含针)。"""
    needle_steps = _spread(NEEDLE_FIRST, NEEDLE_LAST, len(NEEDLE_KINDS), steps)
    needles = [
        Needle(code=needle_code(k), kind=kind, position=needle_steps[k] / (steps - 1), step=s)
        for k, (kind, s) in enumerate(zip(NEEDLE_KINDS, needle_steps, strict=True))
    ]
    by_step = {n.step: n for n in needles}
    stage_at = {
        s: j + 1 for j, s in enumerate(_spread(STAGE_FIRST, STAGE_LAST, STAGES_DONE, steps))
    }
    head: list[BaseMessage] = [
        HumanMessage(content=f"请逐项处理 data/ 下的数据文件,共 {steps} 项,分 10 个阶段推进。"),
        AIMessage(content="明白,我会逐项读取、汇总并写出结果文件。"),
        HumanMessage(content="每完成一个阶段请说明进度。"),
        AIMessage(content="好的。"),
    ]
    middle = [m for i in range(steps) for m in _step_messages(i, by_step.get(i), stage_at.get(i))]
    tail = [m for i in (steps, steps + 1) for m in _step_messages(i, None, None)]
    return Fixture(
        name=name,
        messages=[*head, *middle, *tail],
        needles=needles,
        last_stage=stage_code(STAGES_DONE),
        next_stage=stage_code(STAGES_DONE + 1),
    )


# ---------------------------------------------------------------------------
# 判分
# ---------------------------------------------------------------------------

BUCKETS: tuple[tuple[str, float, float], ...] = (
    ("early", 0.0, 0.35),
    ("mid", 0.35, 0.75),
    ("late", 0.75, 1.0001),
)


def bucket_of(position: float) -> str:
    for name, lo, hi in BUCKETS:
        if lo <= position < hi:
            return name
    raise ValueError(f"position out of range: {position}")


@dataclass(frozen=True)
class Score:
    found: int
    total: int
    by_bucket: dict[str, tuple[int, int]]  # bucket → (found, total)
    by_kind: dict[str, tuple[int, int]]
    progress_ok: bool


def score_text(text: str, fixture: Fixture) -> Score:
    """逐字查编号(大小写敏感,与摘要提示词「原样保留」的要求一致)。"""
    by_bucket: dict[str, tuple[int, int]] = {b: (0, 0) for b, _, _ in BUCKETS}
    by_kind: dict[str, tuple[int, int]] = dict.fromkeys(NEEDLE_KINDS, (0, 0))
    found = 0
    for n in fixture.needles:
        hit = int(n.code in text)
        found += hit
        b, k = bucket_of(n.position), n.kind
        by_bucket[b] = (by_bucket[b][0] + hit, by_bucket[b][1] + 1)
        by_kind[k] = (by_kind[k][0] + hit, by_kind[k][1] + 1)
    progress_ok = fixture.last_stage in text and fixture.next_stage in text
    return Score(found, len(fixture.needles), by_bucket, by_kind, progress_ok)


# ---------------------------------------------------------------------------
# A 组:B-141 之前的拼法(原样拷自 880b5a46 的 orchestrator/context/compressor.py)
# ---------------------------------------------------------------------------

_PRE_B141_PER_MESSAGE_CHAR_CAP = 2_000
_PRE_B141_INPUT_CHAR_BUDGET = 24_000


def _pre_b141_bound_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    head = (max_chars * 2) // 3
    tail = max_chars - head
    dropped = len(text) - max_chars
    return f"{text[:head]}\n[... {dropped} chars truncated ...]\n{text[-tail:]}"


def _pre_b141_role_label(msg: BaseMessage) -> str:
    if isinstance(msg, SystemMessage):
        return "system"
    if isinstance(msg, HumanMessage):
        return "user"
    if isinstance(msg, AIMessage):
        return "assistant"
    if isinstance(msg, ToolMessage):
        return "tool"
    return type(msg).__name__


def format_middle_pre_b141(middle: Sequence[BaseMessage]) -> str:
    """880b5a46 的 ``_format_middle_for_summary``(默认预算)。"""
    from expert_work.common.conversation_channel import is_hidden
    from expert_work.runtime.tokens import flatten_message
    from orchestrator.context.skill_reference import skill_view_reference

    lines: list[str] = []
    for msg in middle:
        if is_hidden(msg):
            continue
        role = _pre_b141_role_label(msg)
        reference = skill_view_reference(msg)
        if reference is not None:
            lines.append(f"{role}: {reference}")
            continue
        text = flatten_message(msg).strip()
        if text:
            lines.append(f"{role}: {_pre_b141_bound_text(text, _PRE_B141_PER_MESSAGE_CHAR_CAP)}")
    return _pre_b141_bound_text("\n\n".join(lines), _PRE_B141_INPUT_CHAR_BUDGET)


# ---------------------------------------------------------------------------
# 跑模型
# ---------------------------------------------------------------------------

Caller = Callable[..., Awaitable[AIMessage]]


@dataclass
class RecordingCaller:
    """包住 compression 路由:记下交给摘要模型的输入与 token 用量。"""

    inner: Caller
    inputs: list[str] = field(default_factory=list)
    usage: list[tuple[int, int]] = field(default_factory=list)
    models: list[str] = field(default_factory=list)

    async def __call__(self, *, messages: Sequence[BaseMessage], tools: Sequence[Any]) -> AIMessage:
        self.inputs.append(str(messages[-1].content))
        response = await self.inner(messages=messages, tools=tools)
        meta = response.usage_metadata
        self.usage.append((meta["input_tokens"], meta["output_tokens"]) if meta else (0, 0))
        self.models.append(str(response.response_metadata.get("model_name", "?")))
        return response


@dataclass(frozen=True)
class Trial:
    fixture: str
    arm: str
    rep: int
    input_chars: int
    input_score: Score
    summary_chars: int
    summary_score: Score
    input_tokens: int
    output_tokens: int
    model: str


def _eval_spec(provider: str, model: str) -> Any:
    from expert_work.protocol.agent_spec import AgentSpec

    return AgentSpec.model_validate(
        {
            "apiVersion": "expert_work.io/v1",
            "kind": "Agent",
            "metadata": {"name": "compaction-retention-eval", "version": "1.0.0", "tenant": "eval"},
            "spec": {
                "tenant_config": {"isolation_level": "dedicated_sandbox"},
                # 同 eval-compress 的主模型配置。
                "model": {
                    "provider": provider,
                    "name": model,
                    "temperature": 0.2,
                    "max_tokens": 32768,
                },
                "system_prompt": {"template": "compaction retention eval"},
                "sandbox": {
                    "runtime": "gvisor",
                    "resources": {"cpu": "0.5", "memory": "512Mi"},
                    "network": {"egress": "none"},
                    "filesystem": {"readonly_root": True, "writable": ["/tmp"]},  # noqa: S108 manifest field
                },
            },
        }
    )


async def build_compression_caller(
    provider: str, model: str, tenant_id: str | None
) -> tuple[Caller, Callable[[], Awaitable[None]]]:
    """在 control-plane pod 里按生产同一取法建 ``compression`` 路由;返回(caller, 收尾)。"""
    from uuid import UUID

    from control_plane.app import _build_secret_store, _build_sql_stores
    from control_plane.platform_secrets import PlatformSecretsService
    from control_plane.settings import Settings
    from orchestrator.agent_factory import build_step_routers

    settings = Settings()
    stores = _build_sql_stores(settings)
    secret_store = _build_secret_store(settings, stores)
    secrets = PlatformSecretsService(store=stores.platform_secret, settings=settings)

    async def provider_keys(name: str) -> list[str]:
        refs = (
            await secrets.effective_provider_secret_refs_for(UUID(tenant_id))
            if tenant_id
            else await secrets.effective_provider_secret_refs()
        )
        return list(refs.get(name, []))  # type: ignore[call-overload]

    routers = await build_step_routers(
        _eval_spec(provider, model),
        secret_store=secret_store,
        provider_key_resolver=provider_keys,
        ignore_api_key_ref=True,
    )
    return routers.compression, stores.engine.dispose


async def run_trial(fixture: Fixture, arm: str, rep: int, inner: Caller) -> Trial:
    from orchestrator.context.compressor import (
        _SUMMARISER_SYSTEM_PROMPT,
        ContextCompressor,
        _split,
    )

    rec = RecordingCaller(inner=inner)
    compressor = ContextCompressor(
        llm_caller=rec,  # type: ignore[arg-type]
        context_window=1_000_000,
        head_keep=HEAD_KEEP,
        tail_keep=TAIL_KEEP,
    )
    if arm == "A":
        middle = _split(fixture.messages, head_keep=HEAD_KEEP, tail_keep=TAIL_KEEP).middle
        summary = await compressor._summarise_with(
            _SUMMARISER_SYSTEM_PROMPT, format_middle_pre_b141(middle)
        )
    else:
        _, wrapped, _ = await compressor._compress_once(list(fixture.messages))
        summary = str(wrapped.content)
    transcript = rec.inputs[-1]
    in_tok, out_tok = rec.usage[-1]
    return Trial(
        fixture=fixture.name,
        arm=arm,
        rep=rep,
        input_chars=len(transcript),
        input_score=score_text(transcript, fixture),
        summary_chars=len(summary),
        summary_score=score_text(summary, fixture),
        input_tokens=in_tok,
        output_tokens=out_tok,
        model=rec.models[-1],
    )


# ---------------------------------------------------------------------------
# 报表
# ---------------------------------------------------------------------------


def _pct(found: int, total: int) -> str:
    return f"{100 * found / total:5.1f}%" if total else "   n/a"


def _sum_pairs(pairs: Sequence[tuple[int, int]]) -> tuple[int, int]:
    return sum(p[0] for p in pairs), sum(p[1] for p in pairs)


def render_table(trials: Sequence[Trial]) -> str:
    """每个(夹具, 组)一行:输入覆盖、摘要保留(总 / 分桶 / 写文件参数)、进度线、token。"""
    header = (
        "fixture arm  n  in_chars in_needles | kept   early    mid    late   args  "
        "progress | sum_chars  in_tok  out_tok"
    )
    rows = [header, "-" * len(header)]
    keys = list(dict.fromkeys((t.fixture, t.arm) for t in trials))
    for fx, arm in keys:
        group = [t for t in trials if (t.fixture, t.arm) == (fx, arm)]
        n = len(group)
        kept = _sum_pairs([(t.summary_score.found, t.summary_score.total) for t in group])
        bucket = {
            b: _sum_pairs([t.summary_score.by_bucket[b] for t in group]) for b, _, _ in BUCKETS
        }
        args = _sum_pairs([t.summary_score.by_kind["args"] for t in group])
        rows.append(
            f"{fx:7} {arm:3} {n:2} {sum(t.input_chars for t in group) // n:9,d} "
            f"{group[0].input_score.found:4d}/{group[0].input_score.total:<5d} | "
            f"{_pct(*kept)} {_pct(*bucket['early'])} {_pct(*bucket['mid'])} "
            f"{_pct(*bucket['late'])} "
            f"{_pct(*args)}  {sum(t.summary_score.progress_ok for t in group)}/{n}      | "
            f"{sum(t.summary_chars for t in group) // n:9,d} "
            f"{sum(t.input_tokens for t in group):7,d} {sum(t.output_tokens for t in group):8,d}"
        )
    total_in = sum(t.input_tokens for t in trials)
    total_out = sum(t.output_tokens for t in trials)
    rows.append(f"total tokens: input {total_in:,d} / output {total_out:,d}")
    return "\n".join(rows)


def _dry_run(fixtures: Sequence[Fixture]) -> None:
    from orchestrator.context import compressor as current

    variant = "B-165" if hasattr(current, "_SUMMARY_TOOL_ARGS_CHAR_CAP") else "B-141"
    print(f"arm B = {variant} formatter from {current.__file__}")
    for fx in fixtures:
        middle = current._split(fx.messages, head_keep=HEAD_KEEP, tail_keep=TAIL_KEEP).middle
        full = current._format_middle_for_summary(middle, char_budget=10**9)
        b_in = current._format_middle_for_summary(middle)
        a_in = format_middle_pre_b141(middle)
        print(
            f"{fx.name}: messages={len(fx.messages)} rendered_after_caps={len(full):,d} "
            f"B_input={len(b_in):,d} needles_in_B={score_text(b_in, fx).found}/12 "
            f"A_input={len(a_in):,d} needles_in_A={score_text(a_in, fx).found}/12"
        )


async def _amain(args: argparse.Namespace) -> int:
    fixtures = [build_fixture(name, FIXTURES[name]) for name in args.fixtures.split(",")]
    _dry_run(fixtures)
    if args.dry_run:
        return 0
    inner, close = await build_compression_caller(args.provider, args.model, args.tenant_id)
    trials: list[Trial] = []
    try:
        for fx in fixtures:
            for arm in args.arms.split(","):
                for rep in range(args.repeats):
                    started = time.monotonic()
                    trial = await run_trial(fx, arm, rep, inner)
                    trials.append(trial)
                    print(
                        f"[{fx.name} {arm} #{rep}] kept {trial.summary_score.found}/12 "
                        f"progress={trial.summary_score.progress_ok} model={trial.model} "
                        f"tokens={trial.input_tokens}/{trial.output_tokens} "
                        f"{time.monotonic() - started:.0f}s",
                        flush=True,
                    )
    finally:
        await close()
    print(render_table(trials))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--fixtures", default="F1,F2")
    parser.add_argument("--arms", default="A,B")
    parser.add_argument("--provider", default="glm", help="主模型厂商(摘要模型按 B-143 取)")
    parser.add_argument("--model", default="glm-5.3", help="主模型型号")
    parser.add_argument("--tenant-id", default=None, help="按该租户的有效凭据解析")
    parser.add_argument("--dry-run", action="store_true", help="只打印尺寸与输入覆盖,不调模型")
    args = parser.parse_args(argv)
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    sys.exit(main())
