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

恢复用例(B-163,`g14` / `g15`):任务中途确定会有一次工具失败(读一个不存在的文件 / 照抄一段对不上的原文去 `edit_file`),
看模型能不能接着把事做完。用例写了 `requires_tool_error: true`:整次一次失败的工具调用都没有(模型绕开了那次失败)
就判「不可判」,与 `requires_compaction` 同一口径。

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
  `EXPERT_WORK_EVAL_EMBED_API_KEY`,模型可用 `EXPERT_WORK_EVAL_RERANK_MODEL` 覆盖),结果缓存在
  `eval-out/kb/rerank_cache.sqlite`(键 = 模型 + 查询 + 候选全文按序 + `top_n`,带任务说明时再加上它),复跑同样的候选零成本;密钥为
  `offline-cache-only` 时缓存没命中直接报错。按 ¥0.5 / 百万 token 记费用,汇总 `rerank` 块里有调用数 / 命中 / token /
  金额。`llm` = 聊天模型排序(`on` 是它的旧名)。**基线分两条**:不开重排写在评测集名下(召回层),开了重排写在
  `<评测集名>@rerank-<模式>` 下(最终排序),互不覆盖。
- **重排对照组(B-206 K-14)**:`--rerank-view dual|single|single_clean`、`--rerank-pool N`、`--rerank-instruct`
  直接设到生产检索器上;不给 = 生产默认 **dual(带标题 / 正文两视图加权)+ 池 100**、不带任务说明。`--rerank-pool 50` =
  K-14 之前的生产,请求逐字相同、重排缓存照样命中。`--rerank-instruct` 只能配单视图(双视图不发任务说明,会被拒)。
  组别记进汇总 `config` 和指纹;非默认组拒绝写基线;某题的重排只判了候选池的一部分(池超过模型单次上限,或超长被拒后
  少送重试)时整轮退出。开发集(主集 303 / 真实问法集 172)上单视图把精确编号组 MRR@10 从 0.917 拉到 0.699,所以默认仍是 dual。
- **留出集** `datasets/kb/huada_v1.holdout.yaml`(精确编号 16 + 无答案 6,只用主评测集没出过题的条目):波 2 期间
  不看结果、不针对它调参,收官时跑一次。
- **为什么基线必须 `--store sql`**:关键词路 SQL 是 `plainto_tsquery` 全 AND,内存库是任一词重叠打分,两套语义不同;
  中文长问句关键词路命中率(设计 §1.3 的固定回归用例)只有 SQL 库量得到。再跑一次 `--method keyword` 单独记这个数。
- **建库规则**:单库,库名 = 评测集 `name`;只入 `--tiers` 里的层(默认评测集 `corpus.tiers`,再默认 `A,D-A`);
  同一 `dup_group` 只入第一份,`--limit-docs` 在去重**之后**截头(汇总里 `skipped_dup` / `skipped_limit` 分开记);
  解析在进程池里跑(PyMuPDF 非线程安全),每份 `--parse-timeout`(默认 120 s),解析完的 Markdown 走
  `ingest_markdown`(与上传入库同一段切块 → 嵌入 → 落段代码);失败按阶段 `parse|embed|store|other` 记进
  `corpus.ingest_failures[]`(文件名、路径、异常类型),文档标 FAILED,整轮继续;同名不同内容的文件(不同目录、不是同一
  `dup_group`)后到的一份以「上级目录/文件名」入库(库内 `(kb_id, filename)` 唯一;评测集按文件名指向的仍是先到的那份),记进
  `corpus.name_collisions[]`(含 `ingested_as`);清单里 0 字节的文件直接跳过(`skipped_empty`);一份都没入成功直接退出,不产出全零的「基线」。
  嵌入器外面包一层计数 / 校维度:返回维度 ≠ `EXPERT_WORK_EMBEDDING_DIM` 直接报错退出;真嵌入**不截断**输入(让 API 自己报错),
  汇总 `corpus.embed` 报送入的文本数 / 字符数 / 超过 8000 字符的条数 / token 数与估算费用(单价 `EXPERT_WORK_EVAL_EMBED_PRICE_CNY_PER_1K`,
  默认 text-embedding-v4 的 0.0005 元/千 token;假嵌入 token 与费用报 0);token 按送入嵌入器的文本算,含缓存命中。
  SQL 库那条路像应用层一样设租户上下文(`build_rls_sessionmaker` + `current_tenant_id_var`),RLS 真开着时也查得到自己的行。
- **锚文本存活检查**:入库后核每条 `expected` 的 `anchor` 是否出现在其文档的某一段里;不存活的 case 单列在汇总 `anchors.dead[]`
  (`document_missing` = 文档没入库,`anchor_not_in_any_chunk` = 切块切断了锚 / 评测集抄错),每行 case 带 `anchors_alive`。
  这些是数据集 / 切块的问题,不是检索的问题,看检索指标时要把它们剔开。
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
- **逐次检索记录(`--search-log <path>`,默认不写)**:每题一行 —— 查询、状态、候选数、是否降级、交给模型的条数与字数,
  以及每个返回段的名次、文件名、段号、段 id、召回路、向量 / 关键词 / 融合 / 重排分数、是否交给了模型(不含正文)。
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
