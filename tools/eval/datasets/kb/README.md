# 知识库评测集(B-175 L1)

给 `kb_eval.py`(L1-T3)用的检索评测集。加载器与校验在 [`../../kb_dataset.py`](../../kb_dataset.py),
语料清点在 [`../../kb_corpus_inventory.py`](../../kb_corpus_inventory.py)。
上位计划:`docs/superpowers/plans/2026-10-13-b175-wave1-plan.md` §L1。

## 文件

```
datasets/kb/
├── README.md              # 本文
├── <name>.layout.yaml     # 语料目录布局(给清点脚本 --layout 用,只有目录 / 文件名)
├── <name>.yaml            # 评测集(L1-T2 生成,如 huada_v1.yaml)
└── <name>.review.md       # 待人工确认清单(自动配不出锚文本的条目)
```

## 清点语料

```sh
uv run --no-sync python -I tools/eval/kb_corpus_inventory.py <资料根目录> <输出目录> \
    --layout tools/eval/datasets/kb/huada_v1.layout.yaml
```

输出 `manifest.csv` + `summary.txt` 到 `<输出目录>`(仓库外)。脚本代码里**没有任何客户目录名**:
哪个顶层目录是聊天记录(Q 层)、哪些是物料(D / D-A / D-B)、要跳过哪些目录和文件,全在 layout 文件里:

```yaml
exclude_dirs: [.workbuddy, ...]   # 不遍历的目录名
exclude_names: [...]              # 按文件名整个排除(→ X)
chat_dir: 聊天记录                 # 顶层目录名 → Q;null = 没有
material_dirs: [...]              # 顶层目录名 → D / D-A / D-B
```

四个键都可省。命令行的 `--exclude-dir` / `--exclude-name` / `--material-dir`(可重复)**叠加**在 layout 之上,
`--chat-dir` 覆盖 layout 里的值。换一套语料就新写一份 `<name>.layout.yaml`,不改代码。

## 格式(YAML)

```yaml
name: huada_v1
corpus: {manifest: ~/kb-eval/huada/manifest.csv, tiers: [A, D-A]}   # 仓库外路径,运行时 --corpus 覆盖
cases:
  - id: faq-0001
    query: 益生菌需要放冰箱冷藏吗？
    source: faq_xlsx | faq_2026 | manual_qa | hard_cross | hard_exact | hard_table | no_answer
    tags: [通用]                      # 产品 / 主题标签,用来分组看指标
    expected:                         # 空列表 = 应「无答案」
      - document: 产品说明书 ：益家人.docx    # 文件名(语料内唯一;重复文件按清单去重后再比)
        anchor: 常温密封保存即可        # 期望段落里一段原文子串(≥8 字),用来判「命中到段」
        page: null                    # P5 之后补
    answer: ...                       # 参考答案(客户给的),只用于 Faithfulness 抽检,不参与检索打分
```

字段:

| 字段 | 必填 | 取值 | 说明 |
|---|---|---|---|
| `name` | 是 | 字符串 | 评测集名,进结果头与基线文件 |
| `corpus.manifest` | 否 | 路径字符串 | `kb_corpus_inventory.py` 产出的 `manifest.csv`;**仓库外**,运行时 `--corpus` 覆盖 |
| `corpus.tiers` | 否 | 字符串列表 | 进评测库的层,默认由运行器决定(基线用 `[A, D-A]`) |
| `cases[].id` | 是 | 字符串,全文件唯一 | 重复报错 |
| `cases[].query` | 是 | 非空字符串 | 用户问法原样,不要改写 |
| `cases[].source` | 是 | 七个枚举之一 | 见下表 |
| `cases[].tags` | 否 | 字符串列表 | 分组看指标用,不参与打分 |
| `cases[].expected` | 是 | 列表(可空) | 空列表 = 「无答案」用例,应拒答 |
| `expected[].document` | 是 | 文件名 | 与 `manifest.csv` 的 `path` 末段(basename)相等 |
| `expected[].anchor` | 是 | ≥ 8 字(去首尾空白) | 期望段落里的**原文子串**,运行器用 `anchor in chunk.content` 判段级命中 |
| `expected[].page` | 否 | 整数或 `null` | 现阶段恒 `null`;P5 加了 `source_locator` 再补 |
| `cases[].answer` | 否 | 字符串或 `null` | 只用于 Faithfulness 抽检 |

## `source` 枚举

| 值 | 含义 |
|---|---|
| `faq_xlsx` | 产品 FAQ 表里的问答 |
| `faq_2026` | `FAQ-2026.xlsx` 里的问答 |
| `manual_qa` | 产品说明书里的 Q&A 段 |
| `hard_cross` | 手写困难样本:答案要跨多份文档拼起来 |
| `hard_exact` | 手写困难样本:精确标识(专利号、菌株编号、规格、活菌数) |
| `hard_table` | 手写困难样本:答案在表格里(零售价、规格表) |
| `no_answer` | 手写困难样本:资料里没有的产品 / 指标 / 竞品,应拒答;`expected` 必须为空 |

## 脱敏规则

- 问题 / 答案 / 锚文本里出现**手机号、微信号、人名**(非产品名、非专家公开署名)的,整条剔除,不改写后保留。
- **聊天记录本期不作为来源**(清单里的 Q 层只用来看问题分布,不生成用例)。
- 评测集里只有问题、答案与文档名 + 一句锚文本;不含任何文档全文。

## 语料不进仓库

- 原文档与 `manifest.csv` 都在仓库外(`~/kb-eval/<语料>/`),**CI 永远不读它们**。仓库里只有 layout(目录名)和评测集(问题 / 答案 / 文件名 / 锚文本)。
- 进 CI 的只有 `test_kb_dataset.py` / `test_kb_corpus_inventory.py`,它们用 `tmp_path` 现造几个小文件。
- 「期望文档在不在语料里」用 `kb_dataset.check_corpus(dataset, manifest_csv)` 本机核一次(只看 A / D-A 层的文件名),结果写进 PR,不进 CI。
