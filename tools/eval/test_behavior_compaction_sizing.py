"""B-141 压缩用例(c 系列)的尺寸复核:按真实 o200k 分词,压缩会不会在任务中途触发、压完放不放得下。

压缩器(``orchestrator/context/compressor.py``)在每次调模型前估算消息正文的 token 数
(``expert_work.runtime.tokens.default_estimator`` = o200k;只数正文,工具调用参数不计),
达到门槛 ``min(窗口 x 0.7, absolute_cap_tokens)`` 就把开头 4 条、末尾 6 条之外的中段写成摘要。
这里用一个保守的累加模型:上下文 ≈ 系统提示词 + 用户消息 + 依次读进来的 fixtures。

- 系统提示词 S 不在仓库里可算(平台会拼接各段),按 1,000~5,000 取两头:10-06 基线 g08 首次
  调用的计费输入(连工具清单)约 5,200。
- 下界(S 小、不计零碎):最晚第 ``latest`` 次读完要越线 —— 否则这条用例多半「不可判」。
- 上界(S 大、每次读另加 300 的零碎):最早第 ``earliest`` 次读完才越线 —— 否则压缩发生在
  任务开头,测不到「压缩之后」的行为。
- 压完放得下:S + 开头那份 + 摘要 3,000 + 末尾 6 条里最多 3 份最大的 < 门槛;放不下 run 会以
  ContextOverflowError 失败,那是假红。
- 最大 3 份 + S 上界 + 最长提示词 < 门槛(只看末尾 6 条也放得下)。
- **并行读**(c-base-1007 的 c04 实锤):模型会在一次调用里并行读同一轮能拿到的多份文件。一轮
  只有「用户消息 + 一次多调用 + 一串结果」时,开头 4 条与末尾 6 条把整轮盖住、中段为空,压缩器
  一遍都压不了(``after 0 compression pass(es)``)。所以同一轮可拿到、提示词又允许一起读的那批
  文件(最多 8 份:开头 2 + 末尾 6),加 S 上界与最长提示词,必须 < 门槛。
"""

from __future__ import annotations

import pytest
import yaml
from behavior_schema import AGENTS_DIR, CASES_DIR, FIXTURES_DIR, Case, load_cases

from expert_work.protocol.agent_spec import AgentSpec
from orchestrator.tools.overflow import EXTERNALIZE_MIN_CHARS

_S_LOW, _S_HIGH = 1_000, 5_000
_PER_READ_OVERHEAD = 300
_SUMMARY_TOKENS = 3_000
_TAIL_BIG_READS = 3

#: 用例 → (读多少份 fixtures(用例级再各轮,按列表顺序), 最早允许第几次读完越线, 最晚,
#: 提示词允许一步里一起读几份(None = 不限,整轮能拿到的都可能并行读))。
_SIZING: dict[str, tuple[int, int, int, int | None]] = {
    # 10 份里第 4~8 份之间越线:压缩后至少还要处理 2 份;提示词要求「每次只处理一份」
    "c01-ten-ledgers-one-by-one": (10, 4, 8, 1),
    # 读到第 2 份菜谱之后、家人推荐(写交付件之前最后一次读)之前或当时越线;每轮点名两份
    "c02-constraints-survive-compaction": (8, 3, 8, 2),
    # 第 7 轮提问之前读 6 部分;第 3~6 步之间越线;一轮一步
    "c03-where-are-we": (6, 3, 6, 1),
    # 第 1 轮的 4 份体检报告读完不越线,第 2~5 轮各拿到一份补充记录、读完越线
    "c04-export-keeps-revisions": (8, 6, 8, None),
}
_KEPT_MESSAGES_READS = 8  # 开头 4 条里至多 2 份结果 + 末尾 6 条


def _encoding():  # type: ignore[no-untyped-def]
    tiktoken = pytest.importorskip("tiktoken")
    try:
        return tiktoken.get_encoding("o200k_base")
    except Exception as exc:  # 离线拿不到分词表:与 runtime 的 tiktoken 测试同样跳过
        raise pytest.skip.Exception("o200k_base BPE unavailable (offline)") from exc


def _cap() -> int:
    raw = yaml.safe_load((AGENTS_DIR / "eval-compress.yaml").read_text(encoding="utf-8"))
    spec = AgentSpec.model_validate(raw).spec
    cc = spec.policies.context_compression
    # glm-5.3 窗口 100 万:门槛就是 absolute_cap_tokens
    return min(int(1_000_000 * cc.threshold_pct), cc.absolute_cap_tokens)


def _crossing(base: int, reads: list[int], overhead: int, cap: int) -> int | None:
    total = base
    for k, tokens in enumerate(reads, start=1):
        total += tokens + overhead
        if total >= cap:
            return k
    return None


def _tokens(enc, name: str) -> int:  # type: ignore[no-untyped-def]
    return len(enc.encode((FIXTURES_DIR / name).read_text(encoding="utf-8")))


def _all_fixtures(case: Case) -> list[str]:
    return [*case.fixtures, *(name for turn in case.turns for name in turn.fixtures)]


def _compaction_cases() -> list[Case]:
    return [c for c in load_cases(CASES_DIR) if c.requires_compaction]


def test_every_compaction_case_has_a_sizing_entry_and_the_compress_agent() -> None:
    cases = _compaction_cases()
    assert {c.id for c in cases} == set(_SIZING)
    assert all(c.agent == "eval-compress" for c in cases)


def test_compaction_fixtures_stay_inline() -> None:
    # 超过外置线的工具结果(bash / exec_python 读出来的)只剩 3,000 字符预览,上下文就不按算式涨了
    for case in _compaction_cases():
        for name in _all_fixtures(case):
            size = len((FIXTURES_DIR / name).read_text(encoding="utf-8"))
            assert size < EXTERNALIZE_MIN_CHARS, (case.id, name, size)


@pytest.mark.parametrize("case_id", sorted(_SIZING))
def test_compaction_triggers_mid_task_and_fits_after(case_id: str) -> None:
    enc = _encoding()
    cap = _cap()
    case = next(c for c in _compaction_cases() if c.id == case_id)
    n_reads, earliest, latest, _batch = _SIZING[case_id]
    reads = [_tokens(enc, name) for name in _all_fixtures(case)[:n_reads]]
    prompts = [len(enc.encode(t.prompt)) for t in case.turns]

    low = _crossing(_S_LOW + prompts[0], reads, 0, cap)
    assert low is not None and low <= latest, (case_id, reads, low)
    high = _crossing(_S_HIGH + sum(prompts), reads, _PER_READ_OVERHEAD, cap)
    assert high is not None and high >= earliest, (case_id, reads, high)

    after = _S_HIGH + reads[0] + _SUMMARY_TOKENS + _TAIL_BIG_READS * max(reads) + max(prompts)
    assert after < cap, (case_id, after)

    top3 = _S_HIGH + sum(sorted(reads)[-3:]) + max(prompts)
    assert top3 < cap, (case_id, top3)


@pytest.mark.parametrize("case_id", sorted(_SIZING))
def test_a_parallel_batch_of_one_turn_never_fills_the_kept_messages(case_id: str) -> None:
    enc = _encoding()
    cap = _cap()
    case = next(c for c in _compaction_cases() if c.id == case_id)
    batch = _SIZING[case_id][3]
    limit = min(batch or _KEPT_MESSAGES_READS, _KEPT_MESSAGES_READS)
    prompts = [len(enc.encode(t.prompt)) for t in case.turns]
    groups = [[*case.fixtures, *case.turns[0].fixtures]] + [t.fixtures for t in case.turns[1:]]
    for index, group in enumerate(groups, start=1):
        worst = sum(sorted(_tokens(enc, name) for name in group)[-limit:])
        total = _S_HIGH + worst + max(prompts)
        assert total < cap, (case_id, f"turn {index}", group, total)


def test_eval_compress_keeps_default_head_and_tail() -> None:
    # 上面的「压完放得下」按开头 4 条(含一份)、末尾 6 条(最多 3 份)算
    raw = yaml.safe_load((AGENTS_DIR / "eval-compress.yaml").read_text(encoding="utf-8"))
    cc = AgentSpec.model_validate(raw).spec.policies.context_compression
    assert (cc.head_keep, cc.tail_keep) == (4, 6)
