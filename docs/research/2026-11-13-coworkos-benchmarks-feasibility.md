# CoworkOS 对比用的公开测评:我们能不能跑(可行性摸底)

调研日期 2026-11-13。只读资料和代码:没花钱、没调模型、没运行任何下载下来的测评代码。

标记:**【已核实】** = 读到了原文、代码或页面;**【推断】** = 由已核实的事实推出来,没有直接证据;**【估计】** = 粗算,没有实测。

## 1. 为什么有这份

RUC-NLPIR 11 月发了 CoworkOS(arXiv 2611.03178,MIT,github.com/RUC-NLPIR/CoworkOS,调研时看的是 `baf03c3`,2026-11-03),用 GLM-5.3 在 12 个公开测评上把自己和 OpenHands、Codex、DeepSeek Harness 放在一起比。我们想知道:如果 Expert Work 也拿同样的测评、同样的模型跑一遍,能不能跑、要做多少事、大概花多少钱、跟他们的数能比到什么程度。

用户 2026-11-13 的决定:

> 「把这次调研记录在案即可,测评后面再跑,具体的规划等跑的时候再决定」

所以本文**只记录调研结果**,不含计划、排期和报价。要跑的时候再按 §6 的问题定方案,再按惯例先试跑测单价、给报价、等批准。

## 2. CoworkOS 是怎么测的

论文分三块【已核实】:

1. **12 个公开测评**,分四类:工具使用(SkillsBench、τ³-Bench Banking、AutomationBench)、生产力(JobBench、Harvey Legal Agent Bench、GDPVal、OfficeQA Pro、WorkspaceBench-Lite)、搜索(WideSearch、BrowseComp)、ML 编程(MLE-Bench-Lite、MLS-Bench)。
2. **24 个真实任务的定性对比**(网站、游戏、Office 演示文稿等,论文图 6),没有打分。
3. **自我改进(RSI)后训练案例**:让 CoworkOS(GLM-5.3)自己做五轮后训练 Qwen3.6-35B-A3B,8 个测评平均分 33.19 → 42.95。

统一设置:GLM-5.3,High 思考档,上下文 256K,单次最多输出 64K(论文 §4.1.1、附录 A.1)【已核实】。

**没有公开的**:评测代码、各测评的适配器、逐题结果;各测评用的版本、LLM 评审模型、用户模拟器模型大多没写;看分数形状是**每个测评只跑了一次**(τ³ 的分数都是 k/97,AutomationBench 都是 k/600)【推断】。

论文表 1 里与本文相关的 6 行(列:OpenHands / Codex / DeepSeek Harness / CoworkOS)【已核实】:

| 测评 | OpenHands | Codex | DeepSeek Harness | CoworkOS |
|---|---|---|---|---|
| SkillsBench | 63.30 | 53.48 | 56.75 | 64.34 |
| τ³-Bench Banking | 40.20 | 46.39 | 55.67 | 49.48 |
| AutomationBench | 50.50 | 30.33 | 53.50 | 51.83 |
| JobBench | 55.02 | 23.42 | 56.15 | 59.88 |
| OfficeQA Pro | 66.20 | 66.30 | 62.30 | 65.90 |
| WorkspaceBench-Lite | 64.54 | 58.81 | 68.76 | 67.14 |

本次只调研了这 6 个,因为它们离我们的场景最近。其余 6 个没展开:两个 ML 编程测评要 H200 GPU;WideSearch / BrowseComp 要他们的搜索后端;GDPVal 的 Elo 基准对手没写清;Harvey 的 120 题样本没公开。

## 3. 六个测评逐个看

**花费的共同假设**:只算被测智能体,按 GLM-5.3 价目 **输入 8 / 缓存命中 2 / 输出 28 元每百万 token**(`tools/eval/kb_agent_cost.py:34`、`tools/eval/clinconsensus.py:58`)【已核实】;缓存命中率按 50%–90% 算,输入折合约 2.6–5 元/百万【估计】。智谱在长任务上的真实缓存命中率没量过,这是最大变量。数据库里平台运行时用的价目表要跑前再核一次(迁移里没有种 glm-5.3 这行)【已核实】。下面都是**单轮**费用;按规矩对外数字至少跑两轮(§5)。

**接入的共同点**:六个测评都没有「调一个 HTTP 智能体服务」的现成适配器【已核实】。我们都要写一段胶水:建 run → 等结束 → 取答案或产物 → 交给官方判分。各测评的「环境」(任务容器、模拟业务系统、用户模拟器)多半要放在我们平台外面,用 MCP 工具接给智能体(原因见 §4)【推断】。

### 3.1 SkillsBench(工具使用)

- **能否拿到**:github.com/benchflow-ai/skillsbench,Apache-2.0,任务全在仓库里,无门槛【已核实】。版本有 `skillsbench@1.0` / `@1.1`(87 题)和论文快照(86 题)【已核实】。**CoworkOS 说的「79 个文本任务」复原不出来**:按 `modality` 去掉图像/视频/音频剩 81,再去 3D 剩 78【已核实统计,结论为推断】。只能自己定口径并写明。
- **接入与工作量**:官方 harness 把智能体装进任务容器里跑,判分也在同一容器里检查文件【已核实】。我们的沙箱挂不了任务镜像,走「工具代理」:写一个 Harbor `BaseAgent`(或 BenchFlow 桥),把智能体的 shell / 读写文件换成 MCP 工具,命令 exec 进外面的任务容器;每题自带的 Skills 要按题装到智能体上。粗估 **5–10 人日**【估计】。
- **运行环境**:要一台 Docker 主机(每题 1–8 核、2–16 GB,无 GPU)【已核实】。几乎所有任务容器要联网,判分时还现场从 astral.sh / PyPI 拉包;部分镜像在 gcr.io 等境外仓库【已核实】。境内要准备 Docker Hub / PyPI / gcr.io 镜像或代理,或把任务主机放境外【推断】。判分基本不用 LLM,没有用户模拟器【已核实】。
- **花费**:按每题 1–5M 输入、2–10 万输出的粗估,79 题单轮约 **¥250–2,200**【估计】;若像我们以前 Harness-Bench 那样每题约 27 万输入,只要约 ¥130【推断】。区间太宽,只能试跑定。另有 Docker 主机的机器钱。
- **可比性风险**:题单对不上;带不带 Skills 没写(官方两种条件平均差约 16 分,64 分量级看像带 Skills)【推断】;版本没写;官方每题跑 3 次取平均,CoworkOS 疑似 1 次;我们换成工具代理后,工具集本身就和 OpenHands / Codex 不同,要写明;国内网络导致的装包失败要单独统计,别算成智能体失败。
- **官方榜**:接受第三方 harness;GLM 只有 OpenHands + GLM 5.1(无 Skills 32.7 / 有 Skills 58.4),**没有 GLM-5.3**【已核实】。
- **与我们场景的关系**:中。考察「按技能说明干活」,我们平台有技能体系;但多数题是编程 / 数据处理,离知识库 + 办公交付件远一些。

### 3.2 τ³-Bench Banking(工具使用)

- **能否拿到**:github.com/sierra-research/tau2-bench,MIT,97 题 + 698 篇文档都在仓库里,能精确选到同一批题【已核实】。**版本是最大的坑**:v1.0.0 → v1.0.1 修了 banking 判分,分数最多上移约 9 分;v2.0.0(现 main)又换了用户模拟器提示词和判官,和 1.x 不可比;文本 banking 的官方榜要用 `v1.0.1` 跑【已核实】。CoworkOS 用哪版没写。
- **接入与工作量**:原生接口是 Python 类,由 tau2 自己执行工具、按轨迹重放判分【已核实】。参考 Harbor 的 tau3-bench 适配器(MCP 边车形态,但基于 v1.0.0 且只用 BM25)写一个 MCP 服务:内含 tau2 环境 + 用户模拟器,对外暴露「给用户发消息 / 收回复」和全部 `alltools` 工具;跑完把记录转回 tau2 消息调官方判分。要关掉我们平台自带的联网搜索、知识库等工具。粗估 **4–7 人日**【估计】。
- **运行环境**:纯 Python,不要 Docker / GPU【已核实】。**要境外模型服务**:用户模拟器(官方 1.x 榜统一用 `gpt-5.2`,换模型就算 custom 提交)、稠密检索 embedding(OpenAI `text-embedding-3-large` 或 OpenRouter `qwen3-embedding-8b`)【已核实】。出网的是评测脚本,不是沙箱。判官影响很小:97 题只有 1 题要 LLM 判官【已核实】。单题墙钟上限 1800 秒,我们平台的排队和沙箱冷启动也算在里面。
- **花费**:Sierra 公开的 GLM-5.2 轨迹每题平均约 134 万输入、3 万输出,长尾单题到 1,200 万输入(会话记录,仓库外);按此 97 题单轮约 **¥200–900**,长尾单题 ¥30–100【估计】。另加用户模拟器(境外,按美元计)和一次性的 embedding 建索引。官方榜建议每题 ≥4 次。
- **可比性风险**:版本、用户模拟器模型、embedding 三样 CoworkOS 都没写;97 题跑一次,一题约 1 分,抖动大;我们平台的系统提示词、平台段注入、子智能体、上下文压缩按官方定义都算 custom。
- **官方榜**:taubench.com 接受第三方提交(分 standard / custom);banking 上 GLM-5.2 = 37.11,**没有 GLM-5.3**【已核实】。
- **与我们场景的关系**:中高。「多轮对话 + 查知识文档 + 调业务工具」和我们的客服 / 知识库类智能体形态接近;但内容是英文银行业务。

### 3.3 AutomationBench(工具使用)

- **能否拿到**:github.com/zapier/AutomationBench,MIT,任务写在代码里【已核实】。仓库没打 tag,但 `v1.0.6` 能精确定位到提交 `6d21054`【已核实】。公开集 6 个领域 × 100 = 600 题,CoworkOS 分数都是 k/600,用的应是这一套【推断】。**PR #13 至今未合并**,只改 `automationbench/tools/api/fetch.py` 一处(把字符串 `"null"` 参数当空对象),手工打上即可复现「v1.0.6 + PR #13」【已核实】。其它修误判的 PR(#35–#40 等)不要打,否则不可比。
- **接入与工作量**:官方 runner 自己就是智能体循环,工具在进程内改内存里的模拟世界,最后按断言比对终态,不用 LLM 判官【已核实】。我们写一个 MCP 服务,每题持有一份模拟世界、暴露 `api_search` / `api_fetch` / `base64_encode`;题目自带的系统提示词原样给智能体。MCP 服务要让平台连得到。粗估 **2–4 人日**【估计】。
- **运行环境**:纯 Python ≥3.13,不要 Docker / GPU / 外网,资源很小【已核实】。
- **花费**:按每题 0.3–1.5M 输入、1–5 万输出,600 题单轮约 **¥640–5,300**,六个里最贵(题最多)【估计】。判分 0 元。
- **可比性风险**:PR #13 必须打;工具集(api / zapier / limited_zapier)和步数口径 CoworkOS 都没写(官方 50 步,我们平台的「步」要定义清楚);公开集本身有不少已知误判;公开集 ≠ zapier.com 榜用的私有集,不能和官方榜比。
- **官方榜**:用私有题集、Zapier 自己跑,没看到第三方提交流程;README 公开集上 GLM 5.2 = 26.17,**没有 GLM-5.3**【已核实】。
- **与我们场景的关系**:中。考的是串多个 SaaS 接口完成业务流程,接近「智能体调业务 MCP」;但全是英文、美国 SaaS。

### 3.4 OfficeQA Pro(生产力)

- **能否拿到**:代码 github.com/databricks/officeqa(Apache-2.0),数据 CC-BY-SA 4.0【已核实】。HF `databricks/officeqa` 要申请(条件:不得用答案训练、不得刷分);**ModelScope 镜像 `evalscope/officeqa` 不用登录**,含 `officeqa_pro.csv`(就是这 133 题)和解析好的语料,共 5.43 GB【已核实】。题目被官方更新过,要钉版本。
- **接入与工作量**:CoworkOS 说按「EvalScope/OfficeQA 原生规则」打分,对应 EvalScope 的 `officeqa_adapter.py`【已核实】。EvalScope 有外部智能体接口(`AgentRunner`:`setup` / `run`),内置 claude-code、codex、deepseek-harness 等;我们写一个 runner 调我们的 HTTP API、取回最终答案即可。**这是六个里最省事的**,粗估 **1–2 人日**【估计】——前提是语料能进工作区(见 §4)。
- **运行环境**:不要 GPU,不要评审 LLM(规则打分),不要外网;数据从 ModelScope 下【已核实】。智能体要在约 697 个解析好的 txt(约 460 MB)里 grep / 读文件、必要时写代码算数【已核实】。灌进知识库也能做,但那是另一种测法,跟 grep 不可比【推断】。
- **花费**:按每题 0.2–1M 输入,133 题单轮约 **¥100–700**【估计】;若像我们短问答用例那样每题 ¥0.1–1,只要 ¥15–130。判分 0 元。
- **可比性风险**:EvalScope 的设置比 Databricks 官方宽松得多——提示里直接告诉了答案在哪个文件,数值容差 1%(官方不提示、容差 0%)【已核实】。所以 CoworkOS 的 62–66 分**不能和官方榜约 61% 比**;要和 CoworkOS 比就原样用 EvalScope 的提示和容差。答案必须包在 `<FINAL_ANSWER>` 里,我们平台的提示词可能改答案格式。EvalScope 外部模式默认单题 600 秒超时,CoworkOS 用的没写。
- **官方榜**:只有 Databricks 自己跑的三家厂商 harness,没有 GLM,没有第三方提交流程【已核实】。
- **与我们场景的关系**:中。最接近「长文档取数 / 知识库问答」,但是英文美国财政表格,不涉及交付件。

### 3.5 JobBench(生产力)

- **能否拿到**:代码 github.com/Job-Bench/job-bench-eval(Apache-2.0),数据 HF `JobBench/job-bench`(MIT,不用登录),ModelScope 有镜像 `evalscope/job-bench`;main split 正好 65 题【已核实】。官方会持续刷新数据,要用 `--revision` 钉 HF 提交;HF 与 ModelScope 版本可能不同步【已核实 / 推断】。
- **接入与工作量**:官方两条路:shell runner(只支持 Claude Code / Codex CLI / OpenCode)或 Harbor 自定义 `BaseAgent`;EvalScope 也有适配器【已核实】。我们写 Harbor `BaseAgent` 或 EvalScope runner:上传每题输入、原样传提示、跑完把产物下载回 output 目录。粗估 **2–4 人日**,联网搜索要先打通【估计】。
- **运行环境**:官方每题 4 CPU / 8 GiB,超时 7200 秒(与 CoworkOS 一致)【已核实】。输入有 csv / txt / xlsx / pdf / db / docx / png,65 题合计约 71 MB;交付物主要是 xlsx / docx / pdf / csv【已核实】。**65 题里 57 题要智能体自己上网找材料**,官方任务容器网络是公开的【已核实】;访问境外站点是硬需求。**评审要境外服务**:官方默认 xAI `grok-4.3`,能换但官方明说换评审会改变分数【已核实】。判分时用 Docker 跑 LibreOffice 重算公式【已核实】。
- **花费**:两种算法差一个数量级——按每题 0.3–2M token 粗估,单轮约 ¥100–750;按我们「1–2 小时长交付件」档(每题 ¥15–140,中位 ¥30–60)算是 ¥1,000–9,000【估计】。论文里其它模型全量一次按标价是 $32–210【已核实】。另加评审(grok-4.3 全量一次约 $20)【已核实】和联网搜索费用。
- **可比性风险**:评审模型(用 grok-4.3 才对得上官方榜,CoworkOS 用谁没写);走 EvalScope 还是官方评审结果会不同(EvalScope 不重算 xlsx 公式,智能体写的公式会读成空)【已核实】;境内网络访问境外站点影响分数;数据公开,联网的智能体可能搜到答案【推断】;官方要求 65 题全部评审成功才算完整成绩。
- **官方榜**:主页榜固定 OpenCode 作 harness、评审 Grok 4.3;**有 GLM-5.3 = 61.4**(第 8)【已核实】。没看到第三方提交流程,但同模型可以直接对照。
- **与我们场景的关系**:**高**。真实白领任务,读 Office / PDF / 表格、交付 xlsx / docx / pdf,和我们的办公交付件场景最像;缺点是英文、美国职业场景、强依赖联网。

### 3.6 WorkspaceBench-Lite(生产力)

- **能否拿到**:代码 github.com/OpenDataBox/Workspace-Bench(MIT);题目 HF `Workspace-Bench/Workspace-Bench-Lite`(100 题,卡片没写许可),工作区 HF `Workspace-Bench/Workspace-Bench-Workspaces`(apache-2.0,英文归档 18.7 GB);都不用登录【已核实】。英文 Lite 100 题能对上。**还有中文版**(题目、工作区、rubric),是六个里唯一能测中文的【已核实】。官方 07-02 改过元数据、08-17 修过一个 rubric 泄露漏洞,要钉 revision【已核实】。
- **接入与工作量**:CoworkOS 说「基于 AgentCompass」,但对应的是**还没合并的 PR #373**,只支持 claude_code / codex 两个 harness,PR 自己说还没和官方分数对齐过【已核实】。官方仓库的接入点简单:加一个 `evaluation/src/agents/expertwork.py`,一个 `run(prompt, work_dir, …)` 函数调我们的 HTTP API【已核实接口,做法为推断】。难点:每题的工作目录是整个人设工作区的完整拷贝(几个 GB),要预置进智能体能浏览的地方;评审要读智能体的执行轨迹,要把我们 run 的工具调用导出成他们的格式【推断】。粗估 **4–7 人日**【估计】。
- **运行环境**:官方每题一个新容器,2 CPU / 8 GiB / 20 GiB 可写(与 CoworkOS 一致),官方默认超时 2000 秒、CoworkOS 用 2 小时【已核实】。不要 GPU。评审是一个 Claude Code 会话,**端点要兼容 Anthropic 协议**;论文用的评审模型是 Seed-2.0-Lite【已核实】。国产模型的 Anthropic 兼容接口能不能用没核实。下载约 19 GB 要走境外或镜像。
- **花费**:论文各配置平均每题约 56 万 token、41 轮【已核实】;100 题单轮约 **¥130–650**【估计】。评审每题 0.1–0.5M token,用便宜模型很低【估计】。
- **可比性风险**:CoworkOS 用的是未合并 PR 或内部改版,评审模型没写,主指标口径(micro / macro 通过率)要对齐,超时不同,轨迹导出不全会吃亏。**和 CoworkOS 可比程度最低**,更适合直接跑中文版测我们自己【推断】。
- **官方榜**:45 个组合,有 GLM-5.1、没有 GLM-5.3;第三方提交入口还没开【已核实】。
- **与我们场景的关系**:**高**。大工作区里的办公文件 + 文件依赖,接近「知识库 + 办公文档」,且有中文版。

### 3.7 一览

| 测评 | 数据能否对齐 | 我们要写的(估计) | 境外依赖 | 单轮花费(估计) | 跟 CoworkOS 可比 | 公开榜有 GLM-5.3 | 场景相关 |
|---|---|---|---|---|---|---|---|
| SkillsBench | 79 题复原不出 | 5–10 人日 + Docker 主机 | 镜像仓库 / PyPI | ¥250–2,200(低可到 ¥130) | 低 | 否 | 中 |
| τ³ Banking | 97 题能对上,版本不明 | 4–7 人日 | 用户模拟器 + embedding(OpenAI) | ¥200–900 + 模拟器 | 中低 | 否 | 中高 |
| AutomationBench | 600 题 + PR #13 能对上 | 2–4 人日 | 无 | ¥640–5,300 | 中 | 否 | 中 |
| OfficeQA Pro | 133 题能对上,钉版本 | 1–2 人日 + 语料进工作区 | 无 | ¥15–700 | 中(照搬 EvalScope 设置) | 否 | 中 |
| JobBench | 65 题能对上,钉版本 | 2–4 人日 + 打通联网 | 评审 grok-4.3 + 境外网站 | ¥100–9,000 + 评审约 $20 | 中高 | **是,61.4** | 高 |
| WorkspaceBench-Lite | 100 题能对上,钉版本 | 4–7 人日 + 大工作区 + 轨迹导出 | HF 下载;Anthropic 兼容评审 | ¥130–650 | 低 | 否 | 高(有中文版) |

## 4. 我们这边的约束

**以前怎么接外部测评**:SpreadsheetBench、Harness-Bench、ClinConsensus 都走对外 API `POST /v1/agents/{agent_code}/runs`(`services/control-plane/src/control_plane/api/agents.py:1704`),每题一个新 `user_id`(= 新沙箱 + 新工作区)【已核实】。只有 ClinConsensus 的脚本在仓库里(`tools/eval/clinconsensus.py`),其余适配脚本当时放在会话临时目录、已丢失。这次的教训:**适配器和逐题结果要进仓库或固定目录**。

**沙箱约束**(决定了「环境放平台外、用 MCP 接进来」的路线)【已核实】:

- **挂不了测评自带的 Docker 任务镜像**:每次领沙箱都用平台唯一的镜像(`services/sandbox-supervisor/src/sandbox_supervisor/supervisor.py:835-840`)。镜像是 python3.12 + LibreOffice + poppler + nodejs 等,没有 docker;根目录只读、非 root,`pip install` 默认装到 `--user`(`infra/sandbox-image/Dockerfile:284-299`),apt 装不了。
- **单条命令最长 300 秒**(默认 60 秒,`services/orchestrator/src/orchestrator/tools/sandbox.py:52,59`),编译 / 大测试集要拆开或放后台。
- **文件进**只能靠上传:`POST /v1/agents/{code}/uploads`(`services/control-plane/src/control_plane/api/external_uploads.py:233`),只收 pdf / docx / xlsx / pptx / txt / md / csv 加图片(`services/control-plane/src/control_plane/api/uploads.py:92-100`),单文件默认 25 MiB(`services/control-plane/src/control_plane/settings.py:348`)。对外 API **没有写工作区的接口**,`external_workspace.py` 只有列文件和下载两个 GET(`:123`、`:206`)。带目录树或其它类型(如 JobBench 的 `.db`)的输入,只能照 Harness-Bench 的老办法:打包 → base64 切成 txt 上传 → 让智能体解包。几百 MB 到几 GB 的语料 / 工作区(OfficeQA、WorkspaceBench)这样传不现实,要么做平台侧预置,要么用 MCP 文件工具从外面供【推断】。
- **文件出**:产物下载 + 工作区单文件下载,客户端已有(`tools/eval/behavior_client.py:286-307`)。
- **时长与步数**:单轮默认 90 分钟,智能体可设到 86400 秒(`settings.py:869-877`,`packages/expert-work-protocol/src/expert_work/protocol/agent_spec.py:1118`);步数默认 30 要调高(`agent_spec.py:786`);可设单次运行 token 熔断兜长尾(`agent_spec.py:1133`)。
- **模型设置和 CoworkOS 不同**:平台里 glm-5.3 登记的是 1M 窗口、最大输出 131072、始终思考(`packages/expert-work-protocol/src/expert_work/protocol/model_catalog.py:547-558`);上下文压缩在 min(窗口 × 0.7, 20 万) 触发(`services/orchestrator/src/orchestrator/context/compressor.py:727,743`)。CoworkOS 是 256K 窗口、64K 输出,对齐时要在评测智能体 manifest 里写明。glm-5.3 不能看图(`model_catalog.py:548`),题目要看图时会走平台看图工具,另计费。
- **并发**:租户沙箱默认上限 50 台(`services/sandbox-supervisor/src/sandbox_supervisor/settings.py:118`),空闲 15 分钟回收;以前三组评测并行撞满上限作废过一批,经验是总量留 30 台以内(会话记录,仓库外)。
- **境外网络**:沙箱出网默认走平台代理(`agent_spec.py:361`),现有评测智能体设成不出网(`tools/eval/datasets/behavior/agents/eval-general.yaml:38`)。10-09 在测试集群实测过:PyPI、arXiv 通,GitHub 超时,HF / Google 不通(会话记录,仓库外)。分两件事看:**智能体要上境外网站**(JobBench 联网搜索)卡在沙箱出网;**评测脚本要调境外服务**(τ³ 的用户模拟器和 embedding、JobBench 的 grok 评审、SkillsBench 的镜像仓库)可以把脚本放境外服务器解决。仓库里没有已提交的境外服务器方案文档。

**能复用的评测代码**【已核实】:

- `tools/eval/behavior_client.py`:SSE 解析与单轮结果(`:91` `SseParser`、`:121` `TurnBuilder`)、`ExternalClient`(`:239`,上传 / 跑一轮 / 下载 / 归档)。
- `tools/eval/b302_live.py:99`(`SpendTracker`:按 `end` 帧计费、缺帧查 `/usage` 补账、过止损线不再开新运行)和 `tools/eval/b302_ab.py`(报价编号 + 止损线才能 `run`,文件头 `:1-22`)。
- `tools/eval/b301_spend.py`:直调模型的逐次账本(先预留再调用),适合评审 / 用户模拟器这类直调。
- `tools/eval/clinconsensus.py`:外部数据集适配样板(`cmd_download` / `cmd_select` / `cmd_bare` / `cmd_platform` / `cmd_judge` / `cmd_score`,`:182-370`;断点续跑 `done_ids` `:168`)。
- `tools/eval/_judge.py`(LLM 裁判)、`tools/eval/kb_agent_estimate.py`(报价脚本)。

## 5. 已有的规矩

| 规矩 | 来源 |
|---|---|
| 10-09 用户定:不再做公开榜单测评,改完善自有评测体系(当时逐个核过 τ³ 等:要境外网络或 OpenAI / Anthropic 判分、许可不许商用、或榜上只有模型行)。用户 11-13 说「测评后面再跑」,要跑时一并确认和这条的关系。 | `docs/superpowers/ROADMAP.md` B-166 行 |
| 测评报告只在公司内部看;以后要外发(投资人 / 客户),先问测评作者或过法务,不外发题目与轨迹 | 用户 10-16 决定(会话记录,仓库外) |
| 和公开榜单对比时,先确认官方成绩用哪一版评分程序,我方用同一版本打分 | `docs/eval-reports/README.md:24` |
| 对外的分数至少同配置跑两轮取平均(同平台两次全量差近 4 分) | 用户 10-17 经验(会话记录,仓库外) |
| 竞品分数要有真实来源和 URL;LLM 评审不用被测模型同厂 | 10-16 会话记录;`tools/eval/README.md` ClinConsensus 节 |
| 先试跑 10–20 题测实际单价,再按「单价 × 题数 × 轮数」给几档报价,等批准再跑全量 | 用户成本规矩(会话记录,仓库外);`b302_ab.py` 的报价编号流程 |
| 评测智能体不是产品:只为抬测评分的提示词改动不做 | 10-16 会话记录 |

另:许可本身看都允许研究使用(Apache-2.0 / MIT / CC-BY-SA 4.0;OfficeQA HF 版有「不得训练、不得刷分」的使用条件;WorkspaceBench-Lite 题目卡片没写许可),外发前照上表规矩处理。

## 6. 届时要决定的事

1. **跑哪几个**:按目的选——要和 CoworkOS / 同模型榜单对照(JobBench 最好,公开榜有 GLM-5.3 = 61.4;OfficeQA Pro 最省事);还是测自己的主场景(WorkspaceBench 中文版);还是两者都要。
2. **和谁比、用什么口径**:跟 CoworkOS 表比(版本、评审、用户模拟器都要猜,只能写明假设),还是跟官方榜比(要按官方版本和评审)。平台原样上(带平台工具和提示词)还是只留测评自带工具。
3. **境外服务器**:要不要临时租(香港 / 新加坡),放哪些东西(评测脚本、任务容器、MCP 服务),智能体本身要不要出境外网(JobBench 联网搜索)。
4. **评审 / 模拟器账号**:xAI(grok-4.3)、OpenAI(用户模拟器 `gpt-5.2`、embedding)、Anthropic 兼容评审端点——开不开、用不用国产替代(替代就要先做交叉校准)。
5. **平台侧要不要先补能力**:大语料 / 大工作区预置、写工作区的对外接口、外接 MCP 环境工具的网络打通、轨迹导出。
6. **预算**:每个测评先试跑多少题(建议 10–20 题)测单价,再定全量轮数(规矩是至少两轮)和止损线。
7. **结果用途**:只内部看,还是可能外发(外发要先问作者或过法务)。

## 附:调研时看的版本

CoworkOS `baf03c3`;SkillsBench `f860ecf`;tau2-bench `c06b07d`(v2.0.0);AutomationBench `4a8e106`(v1.0.6 = `6d21054`);EvalScope `825e934c`;job-bench-eval `660b33a8`(HF 数据 `d43a6657`);Workspace-Bench `3fbd0f1a`(Lite `60b08b1c`);AgentCompass PR #373 `e0eec335`。各测评都在持续更新,要跑时先重新核一遍版本和题单。
