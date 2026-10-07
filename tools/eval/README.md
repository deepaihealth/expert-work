# Eval harness — Stream G.4

A lightweight, Python-native prompt-evaluation harness — a prompt
regression guard. No promptfoo, no Node (Mini-ADR G-3 in
[STREAM-G-DESIGN](../../docs/streams/STREAM-G-DESIGN.md)).

## Run

```bash
python tools/eval/expert_work_eval.py tools/eval/datasets/example.yaml
```

Exits 0 when every case passes, 1 otherwise. The harness is also
exercised by `tools/eval/test_expert_work_eval.py` in the gating `Test
(pytest)` CI job.

## Eval-set format

An eval set is a YAML file — see [`datasets/example.yaml`](./datasets/example.yaml):

```yaml
name: my-eval-set
cases:
  - id: a-unique-id
    prompt: "The input sent to the model."
    mock_response: "Deterministic stand-in output (mock provider / CI)."
    assertions:
      - type: contains       # output contains the value
        value: "expected substring"
      - type: not_contains   # output does NOT contain the value
        value: "sk-"
      - type: regex          # re.search(value, output) matches
        value: '"status"\s*:\s*"ok"'
      - type: equals         # output equals the value exactly
        value: "exact text"
```

## Providers

`run_eval(eval_set, complete)` takes a pluggable provider —
`async def complete(prompt: str) -> str`.

- **`mock_provider(eval_set)`** (M0 / CI) — returns each case's
  `mock_response`. Deterministic, needs no LLM credentials.
- **Real LLM** — supply your own `complete` that calls an LLM. Real
  prompt evaluation needs API credentials, so it runs locally / in the
  M0→M1 Gate, not in CI.

## Scope (M0)

M0 is the harness skeleton: format + runner + assertions + an example
set. Deferred to M2-D: LLM-as-judge assertions, a real provider wired to
the orchestrator LLM stack, regression-gating, A/B. Eval **dataset**
organisation — golden / regression sets — lives in
[`datasets/`](./datasets/README.md) (Stream G.5).

## J.13a baseline aggregator (Stream J.13a, Mini-ADR J-38)

Per-capability eval modules sit next to this harness:

- `memory_recall.py` — J.3 recall / MRR (K12 module).
- `model_routing.py` — J.11 step-class resolution + fallback chain.
- `per_user_isolation.py` — J.14 `caller_owns_thread` decision table.
- (J.1 / J.2 / J.6 / J.15 land in the J.13a-2 PR; 8 other capabilities
  emit `status: DEFERRED` stubs.)

`run_baseline.py` walks the registry and writes the checked-in baseline
file that ``STREAM-M-DESIGN.md`` Exit Criteria reads:

```bash
.venv/bin/python tools/eval/run_baseline.py
# writes tools/eval/baselines/m0_gate_baseline.yaml
```

Each capability module exports `evaluate_set(cases, *, judge=None,
rerun_count=3) -> CapabilityReport` + `load_cases(path)`. The shared
report shape lives in [`_capability.py`](./_capability.py). LLM-judge
defaults (Mini-ADR J-39) are `claude-haiku-4-5-20251001` at
`temperature=0.0` with N=3 reruns, but no J.13a-1 capability uses the
judge — J.1 plan_execute is the only consumer and ships in J.13a-2.

## Offline prompt A/B (`prompt_ab.py`) — Stream HX-5

Compare two manifest variants' `system_prompt` over one eval set
(paired verdicts; the report gives a pass-rate delta + McNemar
discordant counts, no built-in winner threshold):

```bash
python tools/eval/prompt_ab.py \
  --eval-set tools/eval/datasets/example.yaml \
  --spec-a variant_a.yaml --spec-b variant_b.yaml \
  --provider env --llm-model qwen3.5-plus   # EXPERT_WORK_EVAL_LLM_API_KEY / _BASE_URL
```

Variants are manifest YAML files — export a revision snapshot via
`GET /v1/agents/{name}/{version}/revisions/{n}` or the AgentDetail
History tab. `--provider mock` (default) runs the deterministic mock
pipeline (CI exercises that path; real-LLM comparisons are manual).
The JSON artifact lands in `eval-out/prompt_ab_<eval-set>.json`.

## B-140 行为回归评测(在测试环境真跑)

设计:`docs/superpowers/specs/2026-10-05-b140-behavior-regression-eval-design.md`。用例在
`datasets/behavior/cases/`(全部合成),两个评测智能体在 `datasets/behavior/agents/`。

跑在测试环境的对接方租户里(用户 10-05 拍板),用现成的 canary key(Secret `canary-credentials`)。一次性准备:
用控制台登录态把两个评测智能体 manifest 导入该租户(新建智能体只能走控制台平面)。

跑一遍(key 只进环境变量):

```sh
export KUBECONFIG=~/.kube/expert-work-test.yaml
EXPERT_WORK_API_TOKEN="$(kubectl -n expert-work get secret canary-credentials -o jsonpath='{.data.api-key}' | base64 -d)" \
  uv run --no-sync python tools/eval/behavior_runner.py --label base-$(date +%m%d) --note "test <镜像 tag>"
```

对照两次:

```sh
uv run --no-sync python tools/eval/behavior_compare.py eval-out/b140/<改动前>.jsonl eval-out/b140/<改动后>.jsonl --out report.md
```

退出码 1 = 有稳定退步(改动前 ≥2/3 过、改动后 ≤1/3 过)。只换提示词 / 工具 / 模型时,不用发版:建一个改过的评测智能体副本(另一个 code),`--agent-map eval-ahp=eval-ahp-b`。

自证(评测能红):`datasets/behavior/agents/eval-general-broken.yaml` 只改提示词(要求改文件一律整篇重写),导入后
`--only g02-edit-three-places --agent-map eval-general=eval-general-broken`,g02 必须在 `tool_not_used_on` 上判不过。
注意 manifest 里删工具没用:`exec_python` / `bash` / 读写改文件是平台基础能力,每个智能体都有。

压缩用例(B-141,`c01`~`c04`,`--only` 选它们):跑在 `eval-compress`(压缩门槛 3 万 token、只留摘要一道闸),
**要先在控制台导入** `datasets/behavior/agents/eval-compress.yaml`。用例写了 `requires_compaction: true`,整次一次
`compaction` 帧都没有的那次判「不可判」,不计过 / 不过(对照表里写成「2/2(另 1 次不可判)」);每轮与整次的压缩次数
在结果的 `turn_metrics` / `metrics` 的 `compactions` 里。fixtures 的尺寸算式在各用例文件开头,`test_behavior_compaction_sizing.py` 复核。
某一轮才给的资料写在那一轮的 `fixtures` 里(该轮之前上传进同一会话、只附在该轮):用例级 `fixtures` 第 1 轮全都看得到,
模型会一次并行读完,一轮盖满压缩器保留的头尾就压不了(c04 第一版的教训)。
