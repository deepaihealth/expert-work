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
`datasets/behavior/cases/`(全部合成),评测智能体在 `datasets/behavior/agents/`。

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

恢复用例(B-163,`g14` / `g15`):任务中途确定会有一次工具失败(读一个不存在的文件 / 照抄一段对不上的原文去 `edit_file`),
看模型能不能接着把事做完。用例写了 `requires_tool_error: true`:整次一次失败的工具调用都没有(模型绕开了那次失败)
就判「不可判」,与 `requires_compaction` 同一口径。

知识库用例(B-175 P4 多库目录 + 按库检索,`k01`~`k05`,`--only` 选它们):跑在 `eval-kb`,绑三个合成知识库。
**一次性准备**(在跑评测的同一个租户里,控制台知识库页;嵌入费用几分钱):

1. 建三个知识库,名字与描述**逐字**照 `datasets/behavior/fixtures/make_fixtures.py` 的 `KB_BASES`
   (k05 依赖 `b140-kb-hr` 的描述里提到、文档里却没有的「年度体检」):
   `b140-kb-warranty`「家电各品类的保修期限、退换货与维修收费规定(合成数据)」、
   `b140-kb-specs`「各型号家电的技术参数与待机能效分级(合成数据)」、
   `b140-kb-hr`「员工差旅住宿、伙食补助、请假、报销流程与年度体检安排(合成数据)」;其余设置用默认值。
2. 把 `datasets/behavior/fixtures/kb/<库名>/` 下的全部文件上传到同名的库,等文档都变成就绪。
3. 导入 `datasets/behavior/agents/eval-kb.yaml`。`knowledge_base_refs` 的顺序就是系统提示词目录里的句柄
   (K1 = warranty、K2 = specs、K3 = hr),k01 / k02 的 `tool_arg_contains` 按它判,**不要调顺序**。

```sh
EXPERT_WORK_API_TOKEN=… uv run --no-sync python tools/eval/behavior_runner.py --label kb-cand-$(date +%m%d) \
  --only k01-route-specs,k02-route-warranty,k03-cross-base,k04-same-term-two-bases,k05-catalog-not-evidence
```

改动前的版本 `knowledge_search` 没有 `bases` 参数,k01 / k02 的 `tool_arg_contains` 在基线上必然不过;对照时看其余判据
(答对没有、检索次数)与 k03~k05 有没有退步。改 fixtures 要改 `make_fixtures.py` 重新生成,并把库里的文档换成新版。

跨轮清理后的找回用例(B-206 track D,`k06` / `k07`,`--only` 选它们):跑在 `eval-kb-prune`(`eval-kb` 的副本,只绑一本
合成说明书,跨轮清理门槛压到 3,000 / 500、一条都不保留),第 3 轮开头一定把前两轮的 `knowledge_search` 结果清成一行引用。
`k06` 第 3 轮问的数就在第 1 轮检索到的段落里,看模型会不会按引用重读 / 重搜找回来;`k07` 问的是说明书只给相邻型号写了的
参数,看重读之后会不会照邻居的数作答。**一次性准备**(同一个租户;嵌入费用几分钱):

1. 建知识库 `b140-kb-manual`「VT 系列便携储能电源各型号的技术参数与存放保养说明(合成数据)」(名字与描述照
   `make_fixtures.py` 的 `KB_MANUAL_BASE` / `KB_MANUAL_DESCRIPTION`),其余设置用默认值 —— 尺寸复核
   (`test_behavior_kb_prune.py`)按默认设置算:一次检索返回整本说明书,两次就越过清理门槛。
2. 把 `datasets/behavior/fixtures/kb/b140-kb-manual/` 下的文件上传到这个库,等文档变成就绪。
3. 导入 `datasets/behavior/agents/eval-kb-prune.yaml`。

```sh
EXPERT_WORK_API_TOKEN=… uv run --no-sync python tools/eval/behavior_runner.py --label kb-prune-cand-$(date +%m%d) \
  --only k06-recover-pruned-search,k07-pruned-search-not-stated
```

改动前的版本(没有 `knowledge_search` 的引用行)不清理没有工作区的智能体的旧检索结果,`k06` 在基线上等于原样都在、
必然好答;对照时看的是改动后有没有稳定退步。`k06` 的 `turn_tokens_max` 是暂定上限、不是「清理发生了」的证据(基线一次
调用就能答,输入反而可能更少),第一次 3× 真跑之后按实测最大值 ×1.5 改。

多轮指代用例(B-206,`r01`~`r14`,`--only` 选它们):跑在 `eval-coref`,只绑一个合成知识库。测的是平台怎么带着
整段对话把「它 / 那边 / 这份 / 第二个 / 那天 / 那价格呢 / 另一个呢 / 这两个」变成外发的检索参数和答复 —— 平台没有单独的
改写层,主模型看着全部历史写 `knowledge_search.query`,所以放在真跑整段会话的 B-140,而不是单轮的 `kb_agent_eval.py`。
指代对象不限商品:人、客户、商品、组织、文件(某一轮才附上的报价单)、上一条答复的第几项、时间 / 事件、省略属性、复数;
r02 / r05 / r13 / r14 前文有两个同类候选,追问或两个都答算过,只猜一个不算。**一次性准备**(同一个租户):

1. 建知识库 `b140-kb-coref`「员工通讯录、客户档案、在售型号与价格、分部、售后网点与设备巡检记录(合成数据)」,
   把 `datasets/behavior/fixtures/kb/b140-kb-coref/` 下的六份文件传上去、等就绪;
2. 导入 `datasets/behavior/agents/eval-coref.yaml`(开了工作区:r07 / r14 的报价单是逐轮附件)。

```sh
EXPERT_WORK_API_TOKEN=… uv run --no-sync python tools/eval/behavior_runner.py --label coref-base-$(date +%m%d) \
  --only r01-person-he,r02-person-two-candidates,r03-customer-near-name,r04-product-named-by-answer,r05-product-two-candidates,r06-organisation-there,r07-file-this-one,r08-list-item-second,r09-time-that-day,r10-ellipsis-price,r11-ellipsis-the-other,r12-plural-these-two,r13-customer-two-candidates,r14-file-two-candidates
uv run --no-sync python tools/eval/behavior_coref.py eval-out/b140/coref-base-<日期>.jsonl
```

判据 `coref` 只看结果:那一轮回复里出现了**该指的对象**的事实还是**另一个同类候选**的事实(每条事实在合成语料里只出现一次,
`test_behavior_coref.py` 复核),有没有反问 —— 反问只认**问句本身**点了某个候选(判据的 `candidates`)、在问「哪一个 / A 还是 B / 指的是」,或不点名但问「指的是 / 问的是哪位 / 哪款 …」;
「您还想了解哪些信息?」这类客套收尾不算。指代明确时把候选全列出来(混答)不算过,单独计数。`behavior_coref.py` 三个数一起报:**指错对象率**(含「很笃定地答错对象」次数)、
**有两个候选时的追问率**(及「追问或两个都答」率、只猜一个的次数)、**外发检索参数点名率**(参数里写没写对象名、还留着几个
「它 / 这个 / 第二个」这类指示词);另按指代对象类别列通过数。过 / 不过只按回复判,外发参数两项是平台改进要看的信号,不进过 / 不过。

撞上限 / 原地打转用例(B-226,`t01`~`t04` 与 `g16`):`t*` 跑在 `eval-tight`(eval-general 只把步数上限压到 6 步),
**要先在控制台导入** `datasets/behavior/agents/eval-tight.yaml`;10 份台账逐份处理做不完,测撞上限之后的收尾是否如实
(`t01` 没登记、`t02` 登记了一部分)和快撞上限时有没有先把结果登记出去(`t03`)。用例写了 `requires_exit_reason: max_steps`:
最后一轮不是按步数上限结束的就判「不可判」;`t01` 还写了 `requires_no_artifact: 'totals*'`:模型照步数预警提前登记了
totals(那是 `t03` 测的结局)也判「不可判」。`t04` 跑在 `eval-tight-24`(步数上限 24,**也要先导入**
`datasets/behavior/agents/eval-tight-24.yaml`):21 份台账,预警第一次出现时还有台账没算;按 `text_response` 结束却缺台账
= 不过,做完或一直做到步数上限 / token 预算用完 = 过(判据带 `unless_exit_reason: [max_steps, token_budget]`),主 Agent
一条步数预警帧都没有 = 不可判(`requires_step_warning`:客户端逐轮记下每条 `guard` warning / max_steps 帧的 `detail.max`
(`step_warning_maxes`),只认等于该评测智能体 manifest 步数上限的那条,子代的预警不算)。**`t04` 只测发版之后**:旧版本没有
步数预警帧,基线那一遍按设计全是不可判,只看新版本的过 / 不过。文字判据用 `final_text_not_regex` 放过如实的否定句(「已…登记」
中间不跨否定词;「可以下载」「全部 / 所有…做完」看整个分句,分句里有否定词就不算声称);三轮评审的对抗样本钉在
`datasets/behavior/text_check_samples.yaml`(`test_behavior_dataset.py` 逐条判)。正则判不全:出结果前把 t01 / t02 / t03 / g16
每条文字判据的判词(过的和不过的)都人工读一遍。`g16` 让模型反复读一个永远是 PENDING 的文件,写了 `requires_loop_trip: true`:
整次一次原地打转都没被拦下(客户端数 agent 帧里那条隐藏的 loop-detection 提醒,`loop_trips`)就判「不可判」。

## ClinConsensus 健康问答小测(`clinconsensus.py`,2026-10-09)

同一个模型(glm-5.3)、同一段提示词,对比「裸调用」和「放进平台的评测智能体」在中文健康问答上的得分。
题目是 [ClinConsensus](https://arxiv.org/abs/2603.02097)(阿里,EMNLP 2026 Findings)公开的 900 例低难度档,
数据 CC BY 4.0(引用须注明出处),不进仓库,`download` 现拉;每题 30 条判分项,判分提示词逐字取自官方
`docs/judge_prompt.md`,计分与官方 `scripts/eval_cacs.py` 同一公式(判分项通过率 / Pass@10 / CACS@10)。

```sh
uv run --no-sync python tools/eval/clinconsensus.py download
uv run --no-sync python tools/eval/clinconsensus.py select --n 10   # 普通人 / 专业人员各半,固定种子
EXPERT_WORK_EVAL_LLM_API_KEY=... EXPERT_WORK_EVAL_LLM_BASE_URL=https://open.bigmodel.cn/api/paas/v4 \
  uv run --no-sync python tools/eval/clinconsensus.py bare
EXPERT_WORK_API_TOKEN=... uv run --no-sync python tools/eval/clinconsensus.py platform
EXPERT_WORK_EVAL_JUDGE_API_KEY=... EXPERT_WORK_EVAL_JUDGE_BASE_URL=https://api.deepseek.com \
  uv run --no-sync python tools/eval/clinconsensus.py judge
uv run --no-sync python tools/eval/clinconsensus.py score
```

平台组要先在控制台导入 `datasets/clinconsensus/eval-clin.yaml`(用完可删)。判分不要用被测模型同厂商的模型。

**10-09 首次 10 题结果**(DeepSeek v4-pro 判分):裸调用 判分项 47.3% / CACS@10 27.6,平台 48.0% / 28.1,差距在 10 题的波动内;
评测智能体只有基础文件工具、没接医学知识,平台在问答上没有可测增益。10 题里 2 题平台智能体去写文件 / 存产物,
耗时与 token 翻几倍、一题分数更低。花费约 9 元(全量 900 题两组约 800 元);逐题串行裸调用 33 分钟、平台 63 分钟。
与论文成绩对照只能作参考:论文主分数用 GPT-5.1 严格判分,两种判分模型的模型级平均差 8.72 个百分点;
公开题全是低难度档,要对照论文图 5 的低难度档(11 个模型均值 28.5,GLM-5 约 24.0)。

## B-175 知识库检索评测(`kb_eval.py`,走真实入库链路,不进 CI)

计划:`docs/superpowers/plans/2026-10-13-b175-wave1-plan.md` §L1-T3。评测集格式与语料清点见
[`datasets/kb/README.md`](./datasets/kb/README.md)。与老的 `rag.py`(读预切好的段、假嵌入、M0 闸门的一部分)不同,
`kb_eval.py` 把清单里的原文档**原样走一遍上传后的入库链路**(`ingest_document_bytes`:解析 → 语义切块 → 嵌入 → 落段),
再对每条 case 调 `KnowledgeRetriever.search`,所以量到的是产品真实的切块与检索,不是评测自己的近似。

```sh
# 冒烟:内存库 + 假嵌入(关键词桶向量,维度跟 EXPERT_WORK_EMBEDDING_DIM),几秒
uv run --no-sync python tools/eval/kb_eval.py --dataset tools/eval/datasets/kb/<name>.yaml \
    --corpus <清单目录>/manifest.csv --corpus-root <资料根目录> --store memory --embedder fake

# 基线:pgvector 容器(testcontainers,需 DOCKER_HOST)+ 真嵌入;先 --limit-docs 100 跑通再全量
export DOCKER_HOST=unix:///Users/<you>/.docker/run/docker.sock
EXPERT_WORK_EVAL_EMBED_API_KEY=... EXPERT_WORK_EVAL_EMBED_MODEL=text-embedding-v4 \
  uv run --no-sync python tools/eval/kb_eval.py --dataset tools/eval/datasets/kb/<name>.yaml \
    --store sql --embedder real --tiers A,D-A --label baseline-$(git rev-parse --short HEAD) --update-baseline
```

- **可复现**:`--store sql` 默认走精确检索并固定读行顺序 —— 一次性库关掉索引扫描(不用 HNSW)、并行扫描、
  同步顺序扫描与 autovacuum,落库按清单顺序串行。重复文档大量同分,同分谁在前取决于 Postgres 读行的顺序,四样少一样
  结果就会抖(10-14:只关前两样时 303 题里 203 题排序不同,再关同步扫描剩 103 题,再关 autovacuum 为 0)。同一份代码
  连跑两次结果逐条一致(含分数),比较改动前后就用这个默认模式。`--ann` 走生产同款 HNSW,只在要量近似检索本身的召回
  损失时用,结果不保证可复现;指纹里 `vector_search` 记 `exact` / `ann`。
- **语料要齐**:解析超过 `--parse-timeout`(默认 120 秒)后会接着等,总共最多 4 倍才记失败;仍有失败时汇总表打一行
  `WARNING corpus incomplete`。比较两组结果前先看两边 `corpus.chunk_total` 是否相同 —— 10-15 两条评测并行跑,一份大 PDF
  被跳过、少了 4,130 段,当天一批对比全部作废。**评测一次只跑一条**,不要并行。
- **判对规则(`--doc-match`,B-175 波 2 PR0)**:默认 `anchor` —— 一条期望 `{document, anchor}` 的可接受文档 =
  手列的那份 + 入库后任一段含同一锚文本的其他文档(同一份 FAQ 的两个版本之类;10-14 实测产品 FAQ 题有 33 道排第一的
  是没手列的 FAQ-2026,按文件名判就被算错)。锚文本出现在 5 份以上文档里算「太常见」,不扩展、只在汇总
  `anchors.too_common` 里报。`listed` = 只认手列文档(旧规则,对照用)。指纹记 `doc_match`。
- **重排(`--rerank`)**:`dashscope` = 生产同款 `qwen3-vl-rerank`(DashScope 原生接口,密钥沿用
  `EXPERT_WORK_EVAL_EMBED_API_KEY`;换模型对照用 `--rerank-model qwen3-rerank|qwen3.7-text-rerank`(B-206 K-7b,
  只收模型目录里的重排模型,其次才看 `EXPERT_WORK_EVAL_RERANK_MODEL`;换了模型不能 `--update-baseline`;汇总
  `rerank_outcomes` 记每种重排结局的检索次数,`partial` 只计数不退出),结果缓存在
  `eval-out/kb/rerank_cache.sqlite`(键 = 模型 + 查询 + 候选全文按序 + `top_n`,带任务说明时再加上它),复跑同样的候选零成本;密钥为
  `offline-cache-only` 时缓存没命中直接报错。按 ¥0.5 / 百万 token 记费用,汇总 `rerank` 块里有调用数 / 命中 / token /
  金额。`llm` = 聊天模型排序(`on` 是它的旧名)。**基线分两条**:不开重排写在评测集名下(召回层),开了重排写在
  `<评测集名>@rerank-<模式>` 下(最终排序),互不覆盖。
- **重排对照组(B-206 K-14)**:`--rerank-view dual|single|single_clean`、`--rerank-pool N`、`--rerank-instruct`
  直接设到生产检索器上;不给 = 生产默认 **dual(带标题 / 正文两视图加权)+ 池 100**、不带任务说明。`--rerank-pool 50` =
  K-14 之前的生产,请求逐字相同、重排缓存照样命中。`--rerank-instruct` 只能配单视图(双视图不发任务说明,会被拒)。
  组别记进汇总 `config` 和指纹;非默认组拒绝写基线;某题的重排只判了候选池的一部分(池超过模型单次上限,或超长被拒后
  少送重试)时整轮退出。开发集(主集 303 / 真实问法集 172)上单视图把精确编号组 MRR@10 从 0.917 拉到 0.699,所以默认仍是 dual。
- **副本对照组(B-206 K-21②)**:开发集选样时同一 `dup_group` 只入第一份,所以默认跑不出「同一内容换个文件名重传」的样子。
  `--keep-dups real` = 副本都以自己的文件名 / 文档名入库(真实重传的样子;副本的段要真嵌入,花钱);
  `--keep-dups same-title` = 后到的以 `dup<n>/<原件文件名>` 入库、段上带原件的文档名,检索文本与原件逐字相同,嵌入全走缓存(零花费)。
  `--dedup off` 关掉生产检索器的去副本(默认开;键 = 标题路径 + 正文的哈希,只做 NFC、合并空白,不含文档名、不折大小写;
  重排前只去「子段本身相同、且它给出的整块也相同」的子段 —— 共用一条原文的两个版本都留 —— 且只在重排门槛相同的库之间去;
  重排后按整块再去一次)。汇总多一列 `dup@10`(`dup_share@10`:前 10 名里与更靠前一条是同一段 —— 同一把键 —— 的占比,
  有答案 / 无答案的题都算)。看「副本挤占名额」用 `--keep-dups same-title --dedup off` 对 `--keep-dups same-title`。
  `same-title` 配 `--doc-titles shuffled` 时原件的打乱方式与不带副本的那次相同;`real` 配 `shuffled` 会被拒绝。
  副本不占 `--limit-docs` 的名额、随原件去留,所以两臂的原件完全相同。
  两个开关都记进汇总 `config`;非默认组拒绝写基线。
- **关键词路降级(B-206 K-22)**:生产检索在关键词路查不完(语句超时、锁 / 串行化冲突、断连、资源不足;SQL 写错、没权限
  不算,照常报错)时保留向量路的结果并标 `keyword_degraded`;
  评测有一题这样就整轮退出(只剩向量路的数不能当混合检索计分),与查询嵌入失败(K-8)同一处理。
  `kb_agent_eval.py --tally` 的检索次数那行另报 `keyword degraded N`。
- **留出集** `datasets/kb/huada_v1.holdout.yaml`(精确编号 16 + 无答案 6,只用主评测集没出过题的条目):波 2 期间
  不看结果、不针对它调参,收官时跑一次。
- **为什么基线必须 `--store sql`**:关键词路 SQL 是 `plainto_tsquery` 全 AND,内存库是任一词重叠打分,两套语义不同;
  中文长问句关键词路命中率(设计 §1.3 的固定回归用例)只有 SQL 库量得到。再跑一次 `--method keyword` 单独记这个数。
- **建库规则**:单库,库名 = 评测集 `name`;只入 `--tiers` 里的层(默认评测集 `corpus.tiers`,再默认 `A,D-A`);
  同一 `dup_group` 只入第一份(`--keep-dups` 例外,见上),`--limit-docs` 在去重**之后**截头(汇总里 `skipped_dup` / `skipped_limit` 分开记);
  解析在进程池里跑(PyMuPDF 非线程安全),每份 `--parse-timeout`(默认 120 s),解析完的 Markdown 走
  `ingest_markdown`(与上传入库同一段切块 → 嵌入 → 落段代码);失败按阶段 `parse|embed|store|other` 记进
  `corpus.ingest_failures[]`(文件名、路径、异常类型),文档标 FAILED,整轮继续;同名不同内容的文件(不同目录、不是同一
  `dup_group`)后到的一份以「上级目录/文件名」入库(库内 `(kb_id, filename)` 唯一;评测集按文件名指向的仍是先到的那份),记进
  `corpus.name_collisions[]`(含 `ingested_as`);清单里 0 字节的文件直接跳过(`skipped_empty`);一份都没入成功直接退出,不产出全零的「基线」。
  嵌入器外面包一层计数 / 校维度:返回维度 ≠ `EXPERT_WORK_EMBEDDING_DIM` 直接报错退出;真嵌入**不截断**输入(让 API 自己报错),
  汇总 `corpus.embed` 报送入的文本数 / 字符数 / 超过 8000 字符的条数 / token 数与估算费用(单价 `EXPERT_WORK_EVAL_EMBED_PRICE_CNY_PER_1K`,
  默认 text-embedding-v4 的 0.0005 元/千 token;假嵌入 token 与费用报 0);token 按送入嵌入器的文本算,含缓存命中。
  SQL 库那条路像应用层一样设租户上下文(`build_rls_sessionmaker` + `current_tenant_id_var`),RLS 真开着时也查得到自己的行。
- **锚文本存活检查**:入库后核每条 `expected` 的 `anchor` 是否出现在其文档里**检索能返回的某一段**里(B-206 K-1:父段行,
  加上没有父段行、自己就是父段的子段);不存活的 case 单列在汇总 `anchors.dead[]`
  (`document_missing` = 文档没入库,`anchor_not_in_any_chunk` = 切块切断了锚 / 评测集抄错),每行 case 带 `anchors_alive`。
  只看子段的旧口径另报 `anchors.child_level_dead_cases` / `child_level_dead[]`(跨同一父段两个子段的锚文本只在这里算不存活)。
  这些是数据集 / 切块的问题,不是检索的问题,看检索指标时要把它们剔开。「含同一锚文本的其他文档也算对」用同一口径。
- **指标**:文档级命中 = `filename` 相等,段级命中 = `anchor in content`;每条期望只在**最早**命中它的那一名记一次(重叠段重复命中不再加分,
  nDCG ≤ 1),一段含两条锚时两条一起记。按 case 的 `match`(`any` 默认 / `all`,见 `datasets/kb/README.md`)分:
  `doc_hit@10/@20`、`chunk_hit@10/@20`(`any` = 任一期望进前 k;`all` = 全部进前 k)、`doc_coverage@10/@20`、`chunk_coverage@10/@20`
  (进前 k 的期望数 / 期望总数,两种 match 都报;文档级按结果名次切 k)、`mrr@10`、`ndcg@10`(`any` 理想序 1 条相关,`all` 理想序全部期望;
  只算有答案的 case)、`zero_hit_rate`;`no_answer_refusal_rate` / `false_refusal_rate` **只在给了 `--threshold` 时有值**
  (结果为空或全部低于阈值算拒答),没给阈值报 `-`,每行 case 的 `best_score`(最佳向量分数)落在 JSONL 里便于离线扫阈值。
  总体 / 按 `source` / 按每个 `tag`(同一 case 重复 tag 只算一次)各一份。
  注意混合模式下关键词命中总会进融合,所以「无答案」判定要配 `--method vector --threshold <x>`。
- **给模型的文本里的锚文本命中(`context_hit`,B-206「父段级段命中」)**:每题那次检索的前几名交给生产
  `knowledge_search` 工具,经工具节点同一段代码(`_invoke_tool`)渲染成模型拿到的文本(工具默认条数;默认智能体不开工作区,
  不外置截断),锚文本归一后落在里面记 1(`match=all` 要全部锚文本都在);汇总表 `ctx_hit` 列紧挨 `chk_hit@10`。工具改成
  返回父段 / 改条数 / 改格式,这个数自动跟着变。`--top-k` 小于工具要的条数时不算(报 `-`)。
  B-206 K-1 起一条结果是一个父段,段级命中 / `mrr@10` 都按父段文本判;汇总 `tool_output` 报这段给模型的文本每次检索的
  字数 / token 数(均值、P90,围栏和提示都算)和被截成命中子段(`[已截断]`)的结果条数,口径同 `context_hit`。
- **逐次检索记录(`--search-log <path>`,默认不写)**:每题一行 —— 查询、状态、候选数、是否降级、交给模型的条数与字数 / token 数,
  以及每个返回段的名次、文件名、段号、父段号、命中子段号、段 id、召回路、向量 / 关键词 / 融合 / 重排分数、是否交给了模型、
  在工具输出里占的字数 / token 数、是否 `[已截断]`(不含正文;交给了工具但被字数预算挤掉的 = `shown` 真、字数 0)。
- **产出**:`eval-out/kb/<label>.jsonl`(每 case 一行:`match`、`best_score`、`anchors_alive`、返回的 (filename, chunk_index, score, recall_source)、
  命中名次、指标;末尾一行汇总;已存在就拒绝覆盖)、终端一张汇总表;`--update-baseline` 把汇总(去掉 `base_id` / `elapsed_s` / `label`
  这些每次都变的字段)与指纹(评测集 sha256、清单 sha256、`corpus_root`、嵌入模型 / base URL / 维度、store、git commit + dirty 标记、
  tiers、limit_docs、method、top_k、threshold、rerank)并进 `tools/eval/baselines/kb_<name>_baseline.yaml`(`--baseline-out` 可改);
  **有任何入库失败时拒绝更新基线**(退出码非 0,结果 JSONL 照写;同名冲突已改名入库,不算失败)。评测集里 `corpus.manifest` 的相对路径相对评测集文件所在目录解析。
- **凭据只走环境变量**:嵌入 `EXPERT_WORK_EVAL_EMBED_API_KEY` / `_MODEL` / `_BASE_URL`;`--rerank on` 另要
  `EXPERT_WORK_EVAL_LLM_API_KEY` / `_MODEL` / `_BASE_URL`,缺了直接报错退出。真嵌入包在 `CachedEmbedder`
  里(`eval-out/kb/embed_cache.sqlite`,按模型 + 内容哈希缓存),第二次跑近零费用。
- **测试覆盖的边界**:`test_kb_eval.py` / `test_kb_metrics.py` 只跑**内存库 + 假嵌入 + `tmp_path` 现造的小语料**那条路
  (选文档 / 去重 / 同名冲突 / 失败分阶段 / 解析超时 / 维度校验 / 嵌入计数 / 锚存活 / any|all 两级命中 / 拒答 / 分组 / 拒绝覆盖 /
  拒绝写基线 / 指纹字段)。`--store sql`、`--embedder real`、`--rerank on` 三条路**没有测试**,在仓库外手工跑;
  CI 不读真语料、不起容器、不调嵌入 API。

## B-206 知识库智能体层评测(`kb_agent_eval.py`,真模型答题 + 判分,不进 CI)

60 题(主集与真实问法集各取全部「资料没写」题 + 固定种子各抽 15 道有答案题),检索与 `kb_eval.py` 同一条建库链路、库开重排,
工具就是主干的 `knowledge_search`(结果经工具节点渲染);答题模型(默认 glm-5.3)最多搜 3 次,判分模型(默认
deepseek-v4-pro)归成 答对 / 编造 / 正确说没写 / 误拒。一轮约 ¥15;`--dry` 换假模型零花费;`--budget` 到了暂停
(往 `<out>.budget` 写新上限继续、写 `stop` 收尾),结果可续跑;`--tally <out>` 只出统计。用法、环境变量、计费规则见脚本开头说明。
B-206 K-2 起默认同时给模型 `knowledge_read`(与生产一致:绑了知识库的智能体两个工具都有),每题最多读 3 次、不占检索次数,
模型最多回 8 轮;读到的内容记进结果行的 `reads` 并给判分看,`--tally` 另报每题读几次、按读法分布。`--no-read` =
读文档关掉(只给 `knowledge_search`),**不是**改动前的基线:检索结果标头、工具说明、最多回几轮都已是 K-2 之后的;
改动前那一臂要在改动前的检出上用当时的脚本跑。带读文档的一轮约 ¥15~25。

**11-09 尺子修正(B-206)**,设计见 `docs/superpowers/specs/2026-10-21-b206-kb-retrieval-loss-driven-optimization-design.md` §8.2:

- **按结论判 + 多数票**(`kb_agent_judge.py`):判分模型把回答拆成结论,逐条标依据和是不是结论本身,编造由代码算、
  不靠判分模型整体拍板(依据类别、整题标签、引文核对的细则见下面「11-10 尺子」)。每题判 `--judge-votes`(默认 3)次、每票单独缓存,严格过半取多数,否则记「判分分歧」单列。
  结果行有 `label` / `votes` / `agreement`,以及 `yardstick`(尺子版本)/ `judge_votes` / `run_kind`
  (`standard` 或 `hn:<spec 哈希>:recheck<k>` / `…:norecheck`):续跑时这三项与本次不同的 `--out` 直接拒绝。
  11-10 起结果行还记 `gold`(判分看到的含答案段落),以后重判不用重建库。

- **答题循环照生产加提示段**(`kb_agent_prod_prompt.py`,B-206 #2066):产品代码里有 `answer_check` /
  `outgoing_reference` 时,系统提示词照生产加上 `<platform-context>` 说明段与「外发参数」段(按产品的门控函数判),
  本轮有知识库结果显示了段落之后的每次调用,把产品的「答前核对」平台段贴在那次调用视图的最后一条消息末尾(不存)。
  判「显示了段落」直接调产品函数。产品模块不在(改动前的 main)时什么都不加、消息逐字节不变;run 行的
  `prod_prompt_parts` 记这一轮加了哪几段,`--tally` 打出来。
- **限额提示不再施压**:第 4 次检索 / 读文档的提示只说「上限到了、这次没执行」,去掉了「answer with what you have」。
- **第三种标签 `partial`**:资料只写了相关事实、没直接写结论的题(评测集里写 `label: partial` + `related` 原文锚,见
  `datasets/kb/README.md`);合格作答 = B 式(说「资料没直接写」+ 列相关事实 + 推断标明)。
- **头条三个数一起报**(`kb_agent_tally.py`):资料没写的「该拒拒了」率 + 编造题数、有答案的「不该拒却拒了」率、部分写了的
  B 式作答率;再按标签 × 集(main / chat / distractor / hard_negative)细分。`--tally a.jsonl b.jsonl …` 按答题模型分节,
  多个模型另出一张对比表 —— 同一套题换 `--answer-model` 各写一个 `--out` 就是分模型评测。
- **近名干扰题**:标准一轮默认另加 15 道(`--distractors N`,0 = 不加):问句点了名的对象、语料里还有名字只差一个字 /
  同型号骨架的另一份文档;判分被告知那些近名。
- **难负例**(另起一轮,库不同):

  ```sh
  # 1) 本机生成 spec(不花钱;与评测建库同一个解析器,缓存按解析器指纹 + 文件内容做键,别指向 kb_build_dataset 的旧缓存)
  uv run --no-sync python tools/eval/kb_hard_sets.py --dataset tools/eval/datasets/kb/huada_v1.yaml \
      --chat-dataset tools/eval/datasets/kb/huada_v1.chat.yaml --corpus-root <资料根目录> \
      --manifest <清单>/manifest.csv --parsed-cache <新的缓存目录> --n 30 --out eval-out/kb/hard-negatives.yaml
  # 2') 免费空跑核一遍(假模型 + 假嵌入 + 内存库,不连任何接口):解析器护栏 / 删后残留都过了才值得真跑
  uv run --no-sync python tools/eval/kb_agent_eval.py … --hard-negatives eval-out/kb/hard-negatives.yaml \
      --dry --store memory --embedder fake --rerank off --out eval-out/kb/dry-hn.jsonl
  # 2) 删过答案句的库上跑 30 道难负例 + 30 道有答案对照
  uv run --no-sync python tools/eval/kb_agent_eval.py … --hard-negatives eval-out/kb/hard-negatives.yaml \
      --out eval-out/kb/agent-<label>-hn.jsonl
  ```

  删句在解析之后、切块之前,文件名不变;spec 有题号、问题、被删的锚(语料原文)和动到的文档 —— **不进仓库**,生成到
  `eval-out/`(已在 .gitignore)。选题读的文档清单与评测建库同一份;跳过「找全」题、找不到锚的题、与已选的锚**累计**删句后
  会伤到对照题答案的题。评测建库时解析器再核一遍对照锚:删掉任何一条、删后还能找到被删的锚、或对照题取不到含答案段落,
  整轮在答题前就停。锚比对先把 CJK 部首补充 / 康熙部首映射成统一汉字再 NFKC(`kb_text_norm.normalize_cjk`,映射表
  `kb_cjk_radicals.py` 取自 Unicode `EquivalentUnifiedIdeograph.txt`)—— 从 PDF / Office 抽出的文本常混着部首字,NFKC 归一不掉。

  **删完怎么确认答案真的没了(11-10,两道)**:只删锚那一句,答案常以别的措辞、在别的文档或表格行里还留在库里。

  1. *造题时,离线、免费*(`kb_hard_sets.py`):对删过的整份语料穷尽核对 —— 锚不能再出现;参考答案(没有文字答案就取
     被删的锚)里的关键事实(带单位的数字、型号 / 编号)在某一句 / 表格行里出现、且那一句在复述同一件事
     (事实前后各一个锚长的窗口里有 ≥ 六成锚的用字;问句点了对象名时,文档名或那一句里得有这个对象),记「疑似仍可答」,
     默认剔除并接着往下选。spec 的 `excluded` 只记题号和原因码(`fact_number` / `fact_code`),
     `skipped.answer_still_present` 计数;`--keep-suspects` = 照选,只列进 `suspects`。规则是字面的,换了说法的复述抓不到。
     「出现」是在保留分隔符的**原文**句子上把数字 / 型号抽出来比集合(`5g×10袋` 抽出 `5g` 和 `10袋`;`1.5mg` 不算在
     `2.5mg` / `15mg` 里,`KX-305` 不算在 `KX-3050` 里,小数的尾巴 `1.5mg` 的 `5mg` 不当型号),只有定位复述窗口才用
     投影。只合并完整的半角千分位(`1,000`);全角逗号是列举。**已知偏宽**:锚很短(表格单元格、几个字)
     时,六成用字很容易在别的行里凑齐,会多剔除(审查 L8)—— 剔除的题号都在 `excluded` 里,跑之前过一眼。
  2. *评测时、答题前,花判分钱*(`kb_agent_recheck.py`,默认开):删过句的库建好后,每道难负例用与答题同一个
     `knowledge_search`(`limit` = `--recheck-k`,默认 20,上限 20;结果同样受工具 1 万字的文本预算)取前 k 段,
     判分模型对照原题答案 + 被删原文判「这些段落能不能答出这道题」,判 `--judge-votes` 次取严格多数。判为能答的记
     `excluded_answer_still_present`:不答题、不进任何分母,`--tally` 头条单列数量和题号。复核检索报错,或检索降级
     (`embedding_degraded` / `keyword_degraded` / 有库没检索到 `unsearched`,段落不全、判「答不了」不可信)的题不判,
     记 `excluded_recheck_search_failed`,同样不答、不进分母,头条另列;它**不算做完**,同一个 `--out` 续跑时重新复核,
     补上的那行取代失败记录(`--tally` 只认后来那行)。run 行 `recheck` 记
     `{k, checked, excluded, search_errors, search_error_reasons, stopped_by_budget}`。复核的判分钱算进 `--budget`(到了照样暂停、等调高上限;
     写 `stop` 就不再复核、也不再答题);留下来答题的难负例,复核花费记在它那一行的 `cost_cny.recheck`,`--tally` 的
     judge 合计含它,与 run 行一致。`--no-recheck` 关掉;开 / 关、k 不同的结果不能续写到同一个 `--out`(`run_kind` 不同)。
- **免费报价**(不调任何模型):

  ```sh
  uv run --no-sync python tools/eval/kb_agent_estimate.py --dataset … --chat-dataset … \
      --history eval-out/kb/<上一份结果>.jsonl --judge-cache eval-out/kb/agent_judge_cache.sqlite \
      --hard-negatives eval-out/kb/hard-negatives.yaml
  ```

  按历史结果的每题实花估答题 / 重排,判分按新旧提示词长度比 + 结论列表多出的输出 × 票数,难负例轮加删过句文档的重新嵌入
  和答前复核(难负例题数 × 票数 × 一次复核;一次复核 = 历史每次判分的实花 × 复核提示词字数 / 历史判分提示词平均字数,
  与判分同一口径、含思考 token,偏高;`--no-recheck` 不算);打印标准轮、难负例轮、改动前基线与改动后一轮
  (+ `--margin` 余量)。
- **重判已有结果(只换尺子、不重新答题)**(`kb_agent_rejudge.py`):读旧结果的 `answer` / `searches[].content` /
  `reads[].content`,与原来一样拼给评委,用当前尺子重判,写到新文件(每行新 `yardstick`,`rejudged_from` 记原文件、
  原尺子、原类别与原花费;run 行带 `rejudge`)。先免费估价,再跑:

  ```sh
  # 免费:不调任何模型,按提示词字数估、也按原文件判分实花推(后者含看不见的思考 token)
  uv run --no-sync python tools/eval/kb_agent_estimate.py --rejudge eval-out/kb/agent-<label>.jsonl … \
      --judge-cache eval-out/kb/agent_judge_cache.sqlite
  # 花判分钱;难负例轮的文件再加原来那份 --hard-negatives(内容哈希要对得上)
  uv run --no-sync python tools/eval/kb_agent_rejudge.py --from eval-out/kb/agent-<label>.jsonl \
      --out eval-out/kb/agent-<label>.rejudged.jsonl --dataset … --chat-dataset … --corpus-root <资料根目录>
  ```

  11-10 之前的结果行没记 `gold`,有答案题的含答案段落要 `--corpus-root` 在本机用内存库 + 假嵌入重建库取回(不连接口、
  不花钱,几分钟;切块与原跑同一条链路,取回的段落与原跑逐题相同);新结果行记了就不用。`--dry` 换假判分模型空跑;
  `--budget` 到了不再开新题,打印出来、run 行记 `stopped_by_budget`;同一个 `--out` 重跑只补没做完的题,但尺子、判分模型、
  票数、源文件(按解析后的完整路径比,不同目录的同名文件也算不同)、轮次有一项与这次不同就拒绝续写;#2067 之前的
  旧行没有 `run_kind`,按 `standard` 算,重判写的新行都带上 `run_kind`。答前复核剔除 / 失败的行不重判,原样带过去
  (这次花费记 0)。

**11-10 尺子(`SCORING_RULES = 2026-11-10.5`):分级,不再一票否决。** 读结果、复核判分时先看这里。

- **每条结论的依据(`support`)**:

  | 值 | 什么时候用 |
  | --- | --- |
  | `stated` | 资料原文写了。对原文的复述、概括,以及只用原文数字的简单算术(一盒 15 袋、每天 1 袋 → 能吃 15 天)都算 |
  | `inferred_labeled` | 从资料推出来的,回答标明了是推断 |
  | `general_labeled` | 不是来自资料的常识 / 建议,回答说了不是来自资料 |
  | `no_source_needed` | 不需要出处的句子:通用安全提示、免责(「不能替代药物」「不适请咨询医生」)、寒暄。含具体数字、成分、功效、人群、用法的不算 |
  | `inferred_unlabeled` | 从资料推出来的,却当事实说、没标明是推断 |
  | `unsupported` | 资料里没有、也推不出,却当事实说 |
  | `contradicted` | 与资料原文矛盾 |
  | `misattributed` | 资料里有这个事实,但属于另一个对象(另一款产品、另一类人群…),被说成了被问的对象或推广到同类 |

  另标 `core`(布尔):这条是不是直接回答了问题、是关于问题对象的事实性结论本身。转介、安全提示、免责、寒暄一律 false。
- **整题记「编造」只有三种**:有任何 `contradicted` / `misattributed`;`core` 结论是 `unsupported` / `inferred_unlabeled`;
  资料没写的题没说「资料没写」、又有一条没依据的 core 结论(依据不在 `stated` / `inferred_labeled` / `general_labeled` /
  `no_source_needed` 里)。core 断言被标成 `no_source_needed` 的 —— 可能是转介(「建议咨询医生」),也可能是判分把结论
  本身误标了(「可以一起吃」),代码分不出 —— **不判编造**,整题按原样记(资料没写、没说没写的记「其它」),verdict 里
  `core_unsourced` 计数,进头条「待复核·结论本身标了不需要出处」清单(按标签列数量和题号,**所有标签**的题都列:
  资料没写、部分写了、有答案上只要有 core 断言标了不需要出处就在里面),人看。core 结论标了 `stated`
  但引文对不上的,**不算编造**,进「待复核」。
- **难负例:回答复述了被删原文,由评委判**(审查第五轮)。难负例题给评委的说明里列出全部被删原文,判分模板要每条结论
  再填 `repeats_deleted`:这条结论陈述的是被删原文里的内容(不论措辞、数字写法、繁简)填 true,只是复述资料里留下的
  相关句、或留下的资料里也写着的内容填 false;普通题一律 false(普通题上标了 true 的忽略,计 `repeats_deleted_ignored`)。
  `null` 当 false;难负例上没填(缺这个字段或 `null`)的计 `repeats_deleted_missing`。**每票按自己的标记归类**(这一票里
  有一条结论标了 true,这一票就是编造,说没说「资料没写」都一样),**再取严格多数**;结果行的 `verdict` 是多数类别的
  代表票,`vote_repeats` 记每票标了几条。难负例给评委的说明模板(`kb_agent_judge.HARD_NEGATIVE_NOTE`)和近名干扰题的
  说明模板(`DISTRACTOR_NOTE`)与判分模板一起进尺子哈希,改了说明尺子版本自动变。
  **为什么改成评委判**:「引文对上被删句」先后用文字规则换了三种算法 —— 最长连续相同串、用到的不同字、二元组 ——
  每一种都在别的形状上出错:删掉一句后同对象、同句式的相邻句还在,被删锚和相邻句只差一个数字或一个对象字时
  (「每次服用两粒,每日三次」对「每次服用一粒,每日两次」、「甲款冲剂每袋五克」对「乙款冲剂每袋三克」),原样照抄也漏判;
  拒答时改写留下的句子、借用了被删句里几个常用字,又被误判成编造。是不是在说被删的那件事,是语义判断,交给评委。
  文字比对(没找到的引文段和被删锚连续 ≥ 6 字相同,计 `quote_deleted`)只当提示。头条的复核清单(只列题号,不改类别):
  「引文像被删句、评委没标」「评委标了复述被删原文、引文没对上被删句」「标了复述被删原文、但引文在留下的资料里找到了」
  「难负例上评委漏填 repeats_deleted」「有少数票因复述被删原文判编造、整题没判编造」。`.4` 之前的旧结果行没问过评委
  (没有 `repeats_deleted`),引文像被删句的不进「评委没标」,另起一行「旧尺子未问评委」列出。
- **「夹带推断」**:非 core 的 `inferred_unlabeled` / `unsupported` 不再让整题变编造;本来合格(正确说没写 / 答对 / B 式作答)
  的题记「夹带推断」,`--tally` 头条按标签单列,不算进「该拒拒了」等的分子,仍在分母里。误拒 / 答偏 / 其它 / 只说没写照旧。
- **B 式作答**:说了「资料没直接写」+ 至少一条引文核对得上的 `stated` 相关事实(`no_source_needed` 不算相关事实)。
  **已知局限**(审查 L4):引文找不到虽然不再算编造,但部分写了的题上,唯一的相关事实引文对不上时,B 式仍会降成「只说没写」
  —— 看头条「待复核」清单里的部分写了题号。
- **引文核对**:引文和资料过同一个投影(部首归一 + NFKC → 小写 → 只留字母、数字和 `.%≤≥<>±~-`,`.` 只留小数点、
  `-` / `~` 只留在两个字母数字之间;只合并完整的半角千分位 `1,000`,全角逗号是列举;省略号先换成空格;行首的列表编号
  `1. ` / `2、`(`、` 后空格可有可无)先去掉,`1. 5克装` 不会读成 `1.5克`),资料按智能体看到的一段一段分别投影
  (引文不能跨段拼出来),Markdown 表格另按行(表头 + 单元格成对)、按列序列化后一起比。引文比两遍:原样一遍、
  把引文里任意位置的列表编号、资料里行首的列表编号都换成同一个记号(私用区字符 U+E000,投影里留着它;原文里本来
  就有的 `§` 照旧当标点去掉;`1)` / `1）` / `1、` 后面不带空白也认)再比一遍(引文常把几条列表压成一行抄,
  `产品规格: 1. 5克装` / `5克装 2. 10克装`;在原文上换,全角逗号不会被当成千分位;`每日服用1、2次` 里的「1、」换成记号后
  对不上资料里的「每日服用2次」),有一遍找到就算。按省略号切段、每段 ≥ 6 字才核:这一段要在某个视图(一段资料 /
  表格的一行或一列)里找得到,这一段带单位的数字也要在**同一个**视图里 —— 两边都在**原文**上抽出来比集合
  (`5g×10袋`、`1次1袋`、营养成分表的多行、`300mg,VD 5μg` 照抄都找得到;`1.5mg` 不算在 `2.5mg` / `15mg` 里;
  引文段在第一段资料里、数字只在另一段里的,不算找到);太短不核的段,数字对全部资料比。**找不到不再单独导致编造**,只进头条的
  「待复核·引文在资料里没找到」清单(按标签列题号);空或太短的另列。
- **断言级指标**:有依据的断言占比 = (`stated` + `inferred_labeled`)/ 除 `no_source_needed` 外的全部断言,
  头条按集报;`--tally a.jsonl b.jsonl` 多给几份文件时另出一张按文件(臂,完整路径)× 集的表。**只取代表票**(审查 L5):
  多数类别的第一张票,另外两票的断言不算进去。
- **每题检索 / 读文档次数**:分母只算真答了的题,复核剔除 / 失败的题不算。
- **中途中断时 tally 与 run 行对不上**:run 行只在一轮正常收尾时写;Ctrl-C / 进程被杀时已落盘的题照常统计,但没有
  这一轮的 run 行(重排花费、调用次数缺这一段),在跑的那几题已花的钱也没落进任何一行。续跑收尾后补上的 run 行只记续跑那一段。
- **尺子版本**:`SCORING_RULES` 加判分模板哈希;旧尺子(`2026-11-09.3+…`)写的 `--out` 不能接着写,要么换 `--out`,
  要么用上面的「重判」。

## B-302 PR 2 读取规矩 / 页图只留几步 A/B(`b302_*.py`,花钱,不进 CI)

设计 `docs/superpowers/specs/2026-11-11-b302-runaway-reverify-design.md` §5。三个文件:`b302_docs.py` 生成合成文档(6 类 x 2 份 + B-268 回归集,做成手机扫描件),`b302_metrics.py` 是 §5.4 的指标与及格线(纯函数),`b302_ab.py` 是计划 / 报价 / 运行 / 报告。

1. **事故那一类的模板由协调人给**(JSON,格式见 `b302_docs.load_incident_template`);不给就只有 10 份,`b302_ab.py` 拒收。
   `python tools/eval/b302_docs.py --out <dir> --incident-template <模板.json>`
2. **先离线报价**:`python tools/eval/b302_ab.py plan --docs <dir> --doubao-model <§6.2 查到的型号> --doubao-price <输入,输出,缓存读> --stop-line 650 --out-root <历次输出的根目录>` —— 打印每家运行次数(glm 105 / deepseek 93 / 豆包 63)、预计花费、理论上限、实际硬上限、预计模型调用次数,以及这份报价的 `quote_id`(报价内容 + 全部会话的哈希)。**拿这份数找用户批**;`run` 的 `--approved-quote` 必须等于这个 `quote_id`,改了止损线、型号或文档就要重新报价。`--stop-line` 只收 (0, 2000] 的有限数。豆包型号必须在模型目录里、能看图、没下架;单价填 `--doubao-price 输入,输出,缓存读`(元 / 百万 token,三个数都 > 0、输出不低于输入),必须与价目表迁移 `0187_rate_card_dated_model_ids.py` 里这一型号的那一行一致(脚本直接解析那份迁移),价目表里没有的型号拒跑。`--out-root` 会列出该目录下历次运行(每个 `spent.json`)已花的合计 —— **跨报价的总账只在 `plan --out-root` 时显示,`run` 不按它拦**,开跑前自己看一眼。
3. 生成临时智能体:`python tools/eval/b302_ab.py manifests --out <dir> --doubao-model <...>`,控制台导入(三组只差 `policies.reading_rule` / `policies.figure_view_steps`)。**跑完必须删**:`python tools/eval/b302_ab.py cleanup --base-url <测试环境> --doubao-model <同上>` 先翻页列出脚本会生成的那几个确切 code(带租户与数量),确认后加 `--yes` 删(控制台凭据 `EXPERT_WORK_EVAL_CONSOLE_TOKEN`)。这两个覆盖字段在旧镜像上会让整份配置被拒收,回滚前检查见 `docs/runbooks/pending-test-release-acceptance.md` §112.8。
4. 按先后跑,每段都要带批过的报价编号与止损线:`run --phase pilot` → 人工按 §5.3 查 `token_usage` 的全部用量种类(`conversation` 以外不为 0 就停) → `run --phase arm_a` → `report` 看判据 0(复现前提),glm 没复现就停 → `run --phase rest`。凭据走 `EXPERT_WORK_EVAL_API_KEY` / `--base-url`。每一轮开始前判止损线(过线后在跑的会话不再开下一轮);花费按 `end` 帧的 `usage_by_model` 累计(缺 `end` 帧时查 `GET /v1/agents/{code}/runs/{run_id}/usage` 补账)。补账接口说 run 还在跑时先取消、再轮询到终态才记账(等不到就按这一型号每次运行的上限计入);流中途断了同样先取消、再补账,然后把错误抛出。计不了账(两处都没有用量、没有单价的型号、某个会话出错、更新帧里没有 `usage_metadata`)就**阻塞**:不再派新运行,原因写进 `spent.json` 的 `blocked` 与 `report`。查清楚后用同样的参数加 `--unblock "<原因>"` 续跑,原因和被解除的那条记进 `spent.json` 的 `unblocks`。交付件读不了(不是 OOXML)记 `unreadable`、按没有交付件算分,这一轮的轨迹照样保留。单次调用输入超过 25 万 token 就取消那次运行。按 Ctrl-C 中断时,在跑的会话若正在补账(取消后等终态),最多还要轮询 180 秒才退出;补账时网络也断了就按上限计入并阻塞。根本没创建出来的运行(`POST /runs` 被拒,或请求没发出去)、以及正常结束却一步模型调用都没有的运行记 0 并阻塞。中断后用同样的参数再跑同一段即可续跑:已跑完的会话跳过,`report` 按会话去重(留最后一条)。`report` 另报出错 / 被拦下的会话数和交付件来源(登记过 / 只在工作区、没登记 / 没有)。
5. `report` 输出每个模型的判据与结论,以及按 §5.4 末尾规则的落地建议(所有测过的模型都过才改平台默认)。

## B-300 长期记忆来源评测(`b300_*.py`,花钱,不进 CI)

设计 `docs/superpowers/specs/2026-11-11-b300-memory-provenance-design.md` §8(A / B / C 组、§8.5 对比、§8.6 花费与删减顺序、§8.7 C 组)。离线单测 `test_b300_*.py`(假 HTTP、进程内存储、假嵌入)进 CI;真跑不进。

| 文件 | 做什么 |
|---|---|
| `b300_facts.py` / `b300_scenarios.py` | 合成场景:10 条事实(5 个领域 x 2,8 种内容形态,中 / 英两套文字);A 组 12 类 x 10、B 组 7 类 x 10、C 组 5 类 x 5(每领域 1 个,用户 11-13「缩小新题规模,守住 ¥400」),§8.5 对比 20 段。没有租户名、客户内容 |
| `b300_phases.py` / `b300_outcomes.py` | 各段(`a` / `b` / `b-screen` / `c` / `c4` / `c5` / 基线 / 对比)的单元、文件、控制面那几步的输入、版本记录;判分材料与报告汇总 |
| `b300_plan.py` | 报价:每组、每家的调用次数与 ¥ 区间;§8.6 删减顺序;¥400 上限 |
| `b300_spend.py` | 账本(沿用 `b301_spend` 的格式与锁):每次调用 / 每次运行**先按上限预留并落盘,再发**;止损线;跨报价总账 |
| `b300_live.py` | 会花钱的那段:A 组本机进程内提炼、B / C 组对外平面对话、判官 |
| `b300_inpod.py` | 只能在**控制面进程里**做的几步(自包含,不 import 本目录):种记忆、读召回到的记忆 id、C5 / T13 的候选簇记录器(调 `_find_candidate_clusters`,**只出 id**)、只对评测用户整理一轮、查状态 |
| `b300_report.py` | 判分材料、通过线、报告 |
| `b300_approve.py` / `b300_steps.py` / `b300_runs.py` | 报价核对与花钱前的批准(状态目录锁里);`prepare` / `bundle` / `ingest`;花钱的循环与试点量 token |
| `b300_eval.py` | 命令行:`scenarios` / `quote` / `manifests` / `prepare` / `bundle` / `ingest` / `run` / `judge` / `calibrate` / `pilot-tokens` / `report` |
| `b300_tokens.py` | 设计里平台文字的 `o200k_base` token 数(§4.4 / §5.2;tiktoken 第一次用会联网下词表,离线要先设 `TIKTOKEN_CACHE_DIR`) |

**花钱的规矩(用户定)**:上限 ¥400,跨全部报价累计。每次跑前先 `quote`;哪一次会让累计超过 ¥400,先按 §8.6 的顺序删 —— ① B 组备选位置不自动跑、② B1 / B2 在旧提交上的筛选只跑一家、③ C1 / C3 只跑一家(报告写明是哪家、其余没测)—— 删完仍超就 `status: stop_and_ask`,**停下问用户**,不自行再删;用户说跑,把原话用 `--user-said "<原话>"` 传给每个花钱的命令(记进 `<out>/approvals.jsonl`)。`quote` 按每次调用 token 的**高档假设**判会不会超(假设未实测,见报价里的 `token_assumptions`),所以先跑**试点**:`quote --pilot --single-vendor <家> --stop-line <小数>` 单独报价、单独批(每段 2 个单元、一家、各 1 次),用另一个 `--out` 跑完后 `pilot-tokens --out <试点 out> --write tokens.json`,再 `quote --tokens tokens.json` 报正式的价。一份报价用 `--phases b` / `--phases b,c,a` 写明覆盖哪几段(没覆盖的段 `run` / `judge` 直接拒);止损线怎么分给这几段是报价里**写明、由用户批**的一部分(`phase_budgets`:默认按每段低档与高档的中点按比例分,或用户给 `--allocation b=250,c=100,a=50`),某一段超了自己的分预算**只停那一段**,不写进账本、不停整份报价。重报价接着做(用户 11-13 定:先试点、再按实测 token 重报):`progress --out 旧OUT --write progress.json`(记下旧目录里每段**做完了哪些**单元)→ `quote --progress progress.json ...`(按**这份新报价意味着的全部单元**减去做完的算还剩多少,印出 `remaining_by_phase` / `remaining_units_by_phase`)→ 批了以后对每一段 `prepare --phase X --out 新OUT --resume-from 旧OUT`(带同样的报价参数):做完的记录搬进新目录(标 `carried_from_quote`,每条在 `carried.jsonl` 留一行审计,召回 / 状态 / 判分文件一起带过来,报告合着算),只为还没做完的建单元(个数与报价里数的一模一样;用新报价的用户名,只给它们重新种)。旧目录在报价之后又有变动(摘要对不上)就拒,重新 `progress`、重新报价。单元(`units.json`)绑定报价编号,换了报价就要另开 `--out` 重新 `prepare`;试点报价不给 `--stop-line` 时止损线取它自己的高档估计(向上取整),之后的命令要带上报价里印出来的那个数。报价里另列语言配比(`languages`,常量 `b300_scenarios.LANG_CYCLE`,由用户定要不要改)、嵌入的估计(`embeddings`,没有价目,给 `--embed-price` 才折钱)、不计价的项(`not_priced`:沙箱时长等)。

花钱的命令(`run` / `judge` / `calibrate` / `bundle --op consolidate` / `ingest --op consolidate|writeback_usage`)要 `--approved-quote <quote_id>` 与 `--stop-line`(≤ 400,且「别的报价已花 + 止损线」≤ 400,否则拒),并**独占状态目录的锁**(另一个花钱的进程在跑就直接拒);每次预留都按「别的报价已花 + 本报价 + 这一笔」判 ¥400。停下(过线、计不了账)会在账本里写一行 `blocked`,续跑要加 `--unblock "<为什么可以继续>"`(也落账)。改了任何参数报价编号就变。账本在 `~/.cache/b300-eval/<quote_id>.spend.jsonl`,**不能删**;**花钱只有一个状态目录**(`~/.cache/b300-eval`):总账、锁、每个报价的账本都在这里;`--state-dir` 只给测试用(要设 `B300_EVAL_ALLOW_TEST_STATE_DIR=1`,`tools/eval/conftest.py` 会设),平时给了就拒 —— 换目录绕开 ¥400 的路整个没有了。注意这个默认目录跟着 `HOME` 走:换了 `HOME` 就是另一本账、看不到之前花的钱 —— 别换。接着做(`--resume-from`)还要核对版本:旧目录这一段的发布版本、脚本提交必须与这次相同(对话类各段 `prepare` 要带 `--release-tag`;提炼段 `a` / `compare-*` 在本机跑、不打测试环境,**不收** `--release-tag`,给了就拒,版本只看脚本提交),每条搬过来的记录的型号与场景摘要也要与这份报价、现在的场景相同,对不上就整段拒。C5 的种子是 X 加三条仍然有效的相近记忆;C5 ① 没测到的分三种原因各自计数、都不进过线:平台的定时整理在我们这一步之前就整理过了(`c5_consolidated_before_our_step`,它整理出的汇总行照样送判官,判分另列为 `c5_platform_summary_*`)、候选没记录(`c5_candidates_not_recorded`,不判)、整理前没凑成簇(`c5_not_measured`,不判)。只结算的步骤(`ingest --op consolidate|writeback_usage`)认账本里的报价编号、不重算报价,所以有了新报价以后旧报价的预留照样能结。`--user-said` / `--unblock` 要写清原话 / 原因(至少 4 个字符,不收「ok」「好的」这类)。A 组与判官的请求体带产品同一套「关思考 + 输出上限」字段(`agent_factory.thinking_off_request_fields`)。单价:qwen / 豆包取价目表迁移 0187,glm-5.3 / deepseek-v4-pro / glm-5.3-flash 不在迁移里,要 `--accept-documented-prices`(出处写在报价里)。

**凭据只从环境变量读**,不进命令行:对外平面 `EXPERT_WORK_EVAL_API_KEY`(+ `--base-url` 或 `EXPERT_WORK_EVAL_BASE_URL`);A 组各家 `EXPERT_WORK_EVAL_<GLM|DEEPSEEK|QWEN|DOUBAO>_BASE_URL` / `_API_KEY`;判官 `EXPERT_WORK_EVAL_JUDGE_BASE_URL` / `_API_KEY`(deepseek-v4-pro,B-206 尺子);嵌入 `EXPERT_WORK_EVAL_EMBED_*`(同 `kb_eval`;嵌入没有价目,不进账本)。

**在哪跑**:

* A 组、§8.5 对比:本机,进程内直接调生产的写回入口(`flush_messages_with_outcome`),用户消息由生产的 `_build_human_message` 拼。测的是**跑脚本的这个检出目录的代码**:`compare-before` 在 PR 1 合并前那个提交的另一个工作区里跑同一份脚本。
* B / C 组:对外平面,打**当时测试环境上线的那一版**;`run` 必须带 `--release-tag`(如 `f2a21c6b2-test`),连同脚本所在提交记进 `<out>/<段>/meta.json`,换了版本就要换 `--out`。基线(`b-screen`、`c2-baseline` 用 PR 1 合并前的提交;`c4-baseline` / `c5-baseline` 用 PR A 合并前的提交)就是在那一版上线时跑。
* 控制面里的几步:`bundle` 生成自包含脚本,测试环境 `kubectl -n expert-work exec -i "$POD" -- python - < bundle.py > out.jsonl`(同 `compaction_retention.py`),本地栈 `python tools/eval/b300_inpod.py --input step.json`;然后 `ingest --file out.jsonl`。

**控制面里的几步不进 app 的 lifespan**:`b300_inpod.bootstrap` 只拼存储、密钥、租户配置、凭据解析、嵌入、审计、token 用量,不建任何 worker / sweep,装配前后都查没有后台任务(否则这个进程可能去认领真租户排队的运行或写回任务)。

**评测租户的预检**(每个租户一次,对话类各段 `run` 之前必须有):`bundle --op preflight` → pod → `ingest --op preflight`。要求该租户 `memory_purge_enabled` 与 `memory_predictive_review_enabled` 都关,且像个评测租户:对外用户里不是评测脚本建的(`b300-` 开头以外)为 0、智能体里不是评测智能体的(`eval-b300-` 开头以外)为 0(登录控制台的员工只计数、不拦);只出开关与个数,**脚本从不改租户的设置**,要关的由人去关。**整理(凑簇汇总)没有按租户的开关**,平台每 4 小时扫一轮;C5 在取候选时若发现评测用户已经有汇总行(平台先整理了),这一条前提不成立、补跑。种子都在 20 天以内(`MAX_SEED_DAYS_AGO`),离整理与清理的 30 天窗口留足余量。

**一段对话类评测的顺序**(以 `b` 为例;`--tenant-id` 是导入评测智能体的那个租户):

1. `manifests --out agents/` → 控制台导入(每家 `eval-b300-<家>` 不写回、`eval-b300-<家>-wb` 写回)。先人工跑一段确认智能体读得到上传的文件。
2. `prepare --phase b --out OUT --tenant-id T --approved-quote Q` → `OUT/b/units.json` 与 `bundle-seed.py`;pod 里跑它 → `ingest --op seed`。每次运行一个新的评测用户,种子互不串。
3. `calibrate --labels labels.jsonl ...`(只做一次):20 条**人工**标注(每行 `domain` / `label` / `prompt`,5 个领域各 4 条、是否各半),判官一致 ≥ 18 才让 `judge` 开跑。A 组另有一套(`calibrate --set a --labels a-labels.jsonl`,按报价的 A 组票数,默认 1 票);1 票没过就 `quote --a-judge-votes 3` 重新报价、批、再校。
4. `run --phase b --release-tag ... --approved-quote Q --stop-line S` → `bundle --op recalled` → pod → `ingest --op recalled`(召回帧对外已剔除,只能在库里读)→ `judge --phase b`。
5. 种下的那条没被召回的、前提不成立的不计入:`prepare --phase b --reruns` 加一次尝试(补跑合计超过两成就停下问)→ 重复 2、4。出错的那几条不另开尝试,同样的参数再 `run` 就在原地续跑。
6. `report --out OUT --quote-file quote.json`。

C4 分三步:`run --step 1`(写回智能体)→ 等写回 → `bundle --op state --step 1` → pod → `ingest --op state --step 1` → `run --step 2` → 同样查 `--step 2` 的状态 → `run --step 3`(提问)→ 召回 / 判分同上。写回在对外流里看不到用量:`run` 时**在开这段对话之前**按上限预留(预留被拒就不开),之后 `bundle --op writeback_usage` → pod(读 `token_usage` 里这几个评测用户的平台开销行,并报每次运行的写回任务状态,只出数字;时间从这一段**最早**一次 `run` 算起)→ `ingest --op writeback_usage`:一个用户有 run id 的写回任务**全部**结束、且用量读得到,才按实际结算;还有任务没跑完、找不到任务、用量为空,一律**留着预留**(账本里按上限算),过一会儿再 bundle / ingest 一次,从不按 0 结。用量是从最早一次 run 起的累计数,每次只结「累计减去已结」(`writeback-settled.json` 记着),已结过的 run 不再结。对话确定没建起运行(`POST /runs` 回 4xx、连不上、上传就失败了)的那笔写回按 0 结;不知道建没建起的那笔单独留着,不挡同一用户别的几笔。前提(对话二写回后对话一那几条不再有效)不成立的单独计数、补跑。C5:`prepare`(种 X 已被 Y 取代 + 两条相近的)→ `bundle --op candidates`(记录整理前的候选簇,只出 id)→ `bundle --op consolidate`(在 pod 里只对评测用户整理一轮,先核对整理用的是 `glm-5.3-flash`,调用数有上限;每生成一次一个 nonce、一份预留,`ingest` 时按 token 结算,没过结构化校验的调用按 2 次重试的上限补记;同一份脚本的输出收两次会照实补记并停下 —— **一份脚本只跑一次**;pod 里另在 `/tmp/b300-eval-markers`(可用 `B300_MARKER_DIR` 改)留一个按 nonce 的标记,同一份脚本再跑直接拒;pod 重建后标记就没了,这时只剩本地的「收两次」那道)→ `bundle --op state`(汇总行给判官判)→ `run`(四家提问)→ 召回 / 判分同上。

**报告**:每类 x 每家 x 领域判线。B 组线是 11-11 拍板 ①;P6 / P7 / P9 是 §10.2 第 1 条;其余 A 组线是 §8.2 所列;**C 组线一律「待用户确认」,报告只说按提出的线会过 / 不过,不作验收依据**。C1 / C3 只在一家上跑时报告写明哪一家、其余没测;A 组判官默认 1 票(设计没给 A 组算判官钱,本脚本安排),另有 A 组校准把关。基线段报每个场景 3 次里红了几次(≥ 1 留用,0 换掉)。§8.5 对比报写入条数、两类、来源与「下降不超过三分之一」。

**LoCoMo / LongMemEval**:`longmem/endtoend.py` 经提炼入口的显式参数 `user_text_messages` 声明「语料是用户文字」(设计 §4.1);入口还没有这个参数时什么都不传,行为与改前一致;**B-300 的那次 LoCoMo 检查(PR 1 之后的代码)要加 `run_longmem.py --expect-user-text`**,入口没有这个参数就报错,不悄悄跳过。`run_longmem.py` 的端到端档**必须二选一** `--expect-user-text` / `--pre-b300`(都不给、都给都退出);`run_baseline_full.sh` 用 `USER_TEXT_FLAG`(默认 `--pre-b300`,PR 1 合并后改成 `--expect-user-text`)。参数名以 PR 1 为准,改名要同步改 `USER_TEXT_PARAM`。**PR 1 起输入形状变了**:每段会话按角色拆开(用户每轮一条用户消息,经 `user_text_messages` 声明;助手每轮一条助手消息;日期抬头单独一条用户消息),一整段语料一条都没写进去时抛 `NothingIngestedError`。所以 B-300 之前的基线要用新形状重跑、不能沿用旧数;LoCoMo 两个说话人都按用户文字处理,与产品形状不同,分数不能直接和更早的基线比;日期抬头走第一次提炼、可能被丢,时间类题目掉分先查这里(设计 §8.5)。A 组若在被测代码上没看到两次提炼,第一条就停下(不空转花钱)。
