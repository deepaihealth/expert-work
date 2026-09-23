# 文档里的图:从静默丢失到按需取用(B-64)

**状态**:设计定稿(三条待决已于 2026-09-21 定案,见 §14)
**日期**:2026-09-21
**背景条目**:ROADMAP 小 backlog B-64
**涉及**:`read_document` / `ask_image` / 工作区布局 / 沙箱镜像(无新增依赖)

---

## 一、问题

`read_document` 是纯文字抽取:PDF 只取文字层,docx 只取段落与表格单元格,
pptx **只取 `has_text_frame` 的形状**,xlsx 只取单元格值。无 OCR、无视觉模型参与。

两种后果:

1. 扫描件 PDF / 纯图 slide 抽出来是空字符串;
2. 关键信息在图里时文字照常抽出,**模型拿着残缺内容往下做且不知道自己没看见**。

第 ② 条更危险 —— 两边都不报错。用 hermes-agent 源码里那句话说:
*scanned PDF converts "successfully" into silent data loss*。

平台本身有完整的视觉通道(`supports_vision` 的主模型,或 manifest 的 `vision:` 块
加 `ask_image`)。**缺的不是识别能力,是「把文档里的图变成那条通道能消费的东西」
这一步没人做。**

---

## 二、实证

### 2.1 上传件带图率(测试 + 生产合并)

| 类型 | 上传份数 | 带图份数 | 带图率 |
|---|---|---|---|
| **pptx** | 6 | **6** | **100%** |
| **docx** | 20 | **0** | **0%** |
| xlsx | 7 | 0 | 0% |
| pdf | 7 | —(另算) | — |

n 小但分离完全。Fisher 精确检验单尾 **p = 4.3×10⁻⁶**。

> ⚠️ 测试环境 NAS 上的 266 份 OOXML 里 **231 份是 agent 自己产出的**,
> 那批数据说的是「我们生成什么」不是「用户传什么」,不作为本设计的依据。

### 2.2 两环境都为零的三项

| 项 | 测试 | 生产 | 处置 |
|---|---|---|---|
| EMF / WMF | 0 | 0 | 设计已验证能渲(§6.2),不因为当前为零而删掉这条路径 |
| chart XML | 0 | 0 | 20 行的正确性保险,**不拿它论证收益** |
| >25 MB 上传件 | 0 | 0 | `_MAX_DOC_BYTES` 不动 |

### 2.3 pptx 的规模

生产上传件最大 3 张图 / 118 KB。测试环境 agent 产出里出现过 65 张图的 deck ——
不是当前输入形态,但限流设计必须扛得住(§7.2)。

---

## 三、设计原则

1. **静默失效必须变成显式告知。** 「我没看见」和「这里没东西」是两个不同的输出,
   永远不许合并。
2. **前期一张图都不渲。** 只出清单,让模型拿着锚点自己决定花不花钱看。
   ——我们是 agent 循环,模型能自己挑,这是结构性优势。
3. **「模型自己挑」必须配硬预算。** 提示词里写 "do NOT render everything"
   是纪律不是闸门;业界数据显示 agentic 逐页处理可达传统方法的 10–50×。
4. **不开新的生命周期。** 渲染页是平台派生、重算即得的机械产物,
   必须落进**已有的**平台自收垃圾前缀,不新增配额来源。

---

## 四、架构

```
                       ┌─ 无图无表 ─────────→ 今天的纯文字路径,零额外开销
  ① 清单(来源侧)      │
  zip 中央目录 + OOXML ┤
  ~1 ms,不解压正文     └─ 有图/有表
                              │
                              ├─ chart XML ──→ 抽成数据,永不渲染(§9.4)
                              │
                              ▼
                       ② 归一化 soffice --convert-to pdf
                          (0.6 s 小文档 / 9.7 s 22 页重 deck)
                              │
                              ▼
                       ③ figure map:页号 + 显示尺寸 + 前文锚点
                          随正文一起返回,**前置**在 content 头部
                              │
                    ┌─────────┴─────────┐ 模型挑中某页
                    ▼                   ▼
             ④ pdftoppm -jpeg -r 100 -f N -l N   (0.12 s / 62 KB,§7.1)
                    │
                    ▼
             ⑤ 按 agent 能力三分投递(§8.2)
```

**阶段 ① 在 `read_document` 里完成;②③ 推迟到首次渲染请求;④⑤ 是模型的
第二次工具调用。**

「转换推迟」是因为清单与渲染需要的东西不一样:**清单只读 zip 里的 XML
(~1 ms),不需要 LibreOffice**;页号和像素才需要转换。而 pptx 的 slide 与
PDF page 严格 1:1(§6.2 实测),连页号都不用转换就知道。

⇒ **一次 run 如果读了文档但没看图,LibreOffice 一次都不跑。**
那个 22 页 deck 的 9.7 s,只有真要看图时才付。

docx 是例外:段落序号 ≠ 页号,所以它的清单用「在哪句话之后」作锚点而不带
页号,页号在首次渲染时才解析(§9.3)。

---

## 五、判定:图的清单来自来源侧,不来自 PDF

### 5.1 结论

**OOXML 决定「有没有图、是什么图」;PDF 只决定「渲哪一页」。**

### 5.2 为什么不用 PDF 侧反推(实测)

最初设计是用 `pdfplumber.Page.images` 做统一探测器。**实测证伪**:

| 文档 | images | curves | lines | rects |
|---|---|---|---|---|
| docx + PNG 图 | **1** | 0 | 0 | 0 |
| docx + **EMF** 图 | **0** | 7 | 1 | 1 |
| pptx **原生图表** | **0** | 1 | 19 | 2 |
| docx **纯表格** | 0 | **0** | 12 | 0 |

LibreOffice 把 EMF/WMF 和原生图表渲成**原生 PDF 矢量算子**,不是位图 XObject。
`Page.images` 对它们一律为 0 —— 照那个设计发出去,一页全是矢量图表的 slide
会被报成「这里没有图」,**正是本设计要消灭的静默失效,换一层又出现一次**。

矢量侧也补不回来:`curves` 能把表格(全直线)和图形分开,但全直线的柱状图
curves 可能就是 0;而矢量 bbox 并集不可用 —— 图表 slide 算出 `vec_cover=100%`,
实际只是那张满页背景 rect。

### 5.3 来源侧的判据(实测)

`<a:graphicData uri>`:

| 内容 | uri | 处置 |
|---|---|---|
| 图片(PNG/JPEG/**EMF**/WMF 都是它) | `…/drawingml/2006/picture` | 进 figure map |
| 图表 | `…/drawingml/2006/chart` | 抽数据,不渲染 |
| SmartArt | `…/drawingml/2006/diagram` | 进 figure map(未实测,§12) |
| **纯表格** | **无 `graphicData`** | 忽略 —— **零误报** |

关键性质:**PNG 图和 EMF 图的 uri 完全相同**。判定不受媒体格式影响,
这正是我们要的 —— 判「有没有图」的那一步,永远不去碰可能栅格化不了的字节。

pptx 侧 `python-pptx` 给出同等判据:`shape_type` ∈ {`PICTURE(13)`, `CHART(3)`,
`GROUP`, `PLACEHOLDER(14)`, …} 与 `has_chart`。

### 5.4 装饰图过滤

**按显示尺寸,不按原始像素。** 实测:

| | 显示尺寸 | 占页面积 | 原始像素 |
|---|---|---|---|
| 页脚 logo | 29×29 pt | **0.17 %** | 40×40 |
| 血糖图表 | 288×192 pt | **11.4 %** | 600×400 |

阈值:**短边 < 40 pt 或 占页面积 < 1 % 不进 figure map。**

按像素判是错的 —— 一张 2000 px 的 logo 缩到 0.4 英寸摆在页脚,像素很大但人看不见。
显示尺寸来自 `<wp:extent cx cy>`(EMU,914400 EMU = 1 英寸),来源侧免费。

### 5.5 PDF 输入的例外

纯 PDF 没有来源侧。对它保留基于页面的判据:

- `text_chars < 20` 判空页(沿用 hermes 的 `PDF_EMPTY_PAGE_CHARS`);
- 三重阈值防噪:空页 ≥ 2 **且** 占比 ≥ 20 %,**或**绝对 ≥ 10 页。

⚠️ **已知缺口**(§12):born-digital PDF 里的矢量图表探测不到。
扫描页(chars≈0)不受影响。

---

## 六、归一化:LibreOffice → PDF

### 6.1 为什么不抽 media blob

| 理由 | 依据 |
|---|---|
| **Pillow 在沙箱里栅格化不了 EMF/WMF** | 镜像 `7ac31957` 实测 `hasattr(Image.core, "drawwmf")` = **False**。而且 `Image.open()` **会成功**(只读头部拿尺寸),`.load()` 才抛 `OSError: cannot find loader for this WMF file` —— 先骗一下再报错 |
| blob 丢上下文 | Word 里裁过的图,blob 是未裁的原图;浮动图层的叠压关系没了;SmartArt 根本不在 `media/` 里 |
| slide 是设计过的视觉单元 | 抽出单张图,位置、标注、箭头指向全没了 |

### 6.2 实测

| 项 | 结果 |
|---|---|
| EMF / WMF 嵌进 docx 转 PDF | **成功**,0.69 s / 0.60 s,与 PNG 基线(0.62 s)无差 |
| EMF 内的文字 | 进了 PDF 文字层(`chars` 29 → 42)。**Excel 图表粘进 Word 的轴标签、数据标签、图例不看图就能拿到** |
| pptx slide ↔ PDF page | **严格 1:1**(3 slides → 3 pages) |
| 纯图 slide | `chars = 0` —— hermes 的空页判据**原样适用于 pptx** |
| CJK | 通过(镜像有 `fonts-noto-cjk` + `zh_CN.UTF-8`) |
| 小文档耗时 | docx 0.65 s / pptx 0.72 s(首次 1.96 s 建 profile) |
| 22 页 / 65 图 / 47.8 MB | 转换 **9.4 ~ 9.7 s**,峰值内存 **286 MB**(cgroup 2048 MB) |

### 6.3 三个必须写进实现的坑(都是实测撞到的)

1. **soffice 输出名按 basename 派生。** 同目录下 `report.docx` 与 `report.pptx`
   一起转 → 后者**静默覆盖**前者的 `report.pdf`。**每次转换用自己的 outdir。**
2. **pdftoppm 页号按总页数补零。** 22 页文档出的是 `page-01.png` 不是 `page-1.png`。
   **必须 glob,不许拼文件名。**
3. **首次调用要建用户 profile**(1.96 s vs 后续 0.65 s)。沙箱是热复用的,
   第一次转换的耗时预算要按冷算。

### 6.4 依赖

**零新增。** 沙箱镜像 `7ac31957` 实测已有:

```
soffice / libreoffice / pdftoppm / pdftotext / pdfinfo
python-docx 1.2.0  python-pptx 1.0.2  openpyxl 3.1.5
pdfplumber 0.11.10  pdf2image  Pillow 12.3.0
```

(`PyMuPDF` 不在,也不需要 —— poppler 覆盖了渲染与页信息。)

---

## 七、渲染与预算

### 7.1 格式与分辨率

实测(照片密集页):

| 格式 | DPI | 耗时 | 文件 | base64 |
|---|---|---|---|---|
| png | 150 | 0.26 s | 1124 KB | 1498 KB |
| **jpeg** | **150** | **0.16 s** | **173 KB** | **231 KB** |
| jpeg | 100 | 0.12 s | 62 KB | 83 KB |
| jpeg | 200 | 0.20 s | 357 KB | 476 KB |

JPEG 比 PNG 小 6.5×,这条没有争议。**但 150 dpi 是错的。**

Anthropic 的建议上限是**长边 1568 px 且约 1.15 MP**;超了服务端自己缩,
「多花字节、拖慢首 token,一点分辨率都不多」。hermes 的
`_EMBED_MAX_DIMENSION = 1568` 就是这个数。

四种页型算下来:

| 页型 | 100 dpi | 150 dpi |
|---|---|---|
| pptx 16:9 (13.33×7.5 in) | 1333×750 = **1.00 MP** ✅ | 2000×1125 = 2.25 MP ❌ |
| pptx 4:3 (10×7.5 in) | 1000×750 = **0.75 MP** ✅ | 1500×1125 = 1.69 MP ❌ |
| docx Letter | 850×1100 = **0.94 MP** ✅ | 1275×1650 = 2.10 MP ❌ |
| docx A4 | 827×1169 = **0.97 MP** ✅ | 1240×1754 = 2.17 MP ❌ |

**150 dpi 在四种页型上全部超线** —— 2.8 倍的字节换零有效分辨率。

**默认 `-jpeg -r 100`**(62 KB / 约 1000 图像 token)。

⚠️ 待验:正文小字在 100 dpi 下 VLM 还认不认得出(10.5 pt 正文 ≈ 15 px 高)。
进观察项 §13.3。若不够,提高 dpi 的同时必须**同步降低渲染区域**
(只渲图所在的那一块而非整页),不能靠超过 1.15 MP 去换清晰度 —— 那一档是
服务端缩掉的,花了也拿不到。

### 7.2 两套预算,不是一套

hermes 的源码注释点破了为什么必须分开:

> *Proactive embed cap: **this image is re-sent on every later turn**, so resize
> DOWN to the history-reuse target ... those are one-shot viewing limits —
> **history embeds are sized smaller so repeated turns don't blow the context***

它的两个数相差 78 倍:

```python
_MAX_BASE64_BYTES    = 20 * 1024 * 1024   # 一次性看图
_EMBED_TARGET_BYTES  = 256 * 1024         # 进历史的
_EMBED_MAX_DIMENSION = 1568
```

#### 渲染预算(一次调用内)

- 单次调用:`MAX_PAGES_PER_CALL`(起手 3;100 dpi 下 ≈ 3 MP,与 openclaw
  `PDF_MAX_PIXELS = 4M` **每次调用**同一量级)
- 超了直接拒并说明原因,不静默截断

> **修订(2026-09-23,用户拍板):删掉「单 run 累计」上限。**
> 初稿这里还有三条:单 run 累计 `MAX_RENDER_PIXELS = 12M`(≈12 页 @100 dpi)、
> 塞不下时降分辨率不丢页(openclaw `resolveRenderPlan` 的二分)、预算耗尽明确告知。
> 删掉的理由:
>
> - 12M 是**猜的**,没有任何数据;
> - 两份参考实现**没有一家**做单 run 累计 —— openclaw 的 `PDF_MAX_PIXELS = 4M`
>   是**每次调用**的,hermes 按页数限、同样是每次调用。初稿写「抄 openclaw 的
>   像素预算」,抄的是形状,却把**作用范围**从每次调用换成每 run 累计、**数字**
>   从 4M 改成 12M,两处都没写理由;
> - 已有两道限都与参考实现对齐:每次调用 3 页、驻留滑窗 3 张(两家都是 3);
> - 累计上限唯一护得住的是一个 run 里的沙箱渲染总时长,那已被 run 的 deadline 与
>   步数上限兜住;代价却是用户看得见的 —— 合法的长扫描件翻到第 12 页被拦下。
>
> 上线后按 §13.3 数真实 run 实际渲多少页,真有失控再按数据定。

#### 历史驻留预算(Path A 专有,见 §8.5)

- `FIGURE_EMBED_MAX_BYTES = 256 KB`、`FIGURE_EMBED_MAX_DIMENSION = 1568`
- 100 dpi 的渲染物天然落在里面;这两个值是**上限护栏**,不是常规路径

`MAX_PAGES_PER_CALL = 3` 的起手值是猜的(量级对齐 openclaw 每次调用);
256 KB / 1568 **原样**照搬 hermes 的 `_EMBED_TARGET_BYTES` / `_EMBED_MAX_DIMENSION`。
都进验收观察项(§13.3)。

---

## 八、投递

### 8.1 约束:图只能走 HumanMessage,不能走工具结果

两家参考实现都把图放进**工具结果**(hermes 的 `_multimodal` 封套 /
openclaw 的 pi `ImageContent`)。**我们不能照抄**,而且原因不是我们的
`ToolResult.content` 是 `str` —— 那只是表象,真正的约束在协议层:

| 通道 | Anthropic | OpenAI / GLM / Kimi / DeepSeek / Qwen / Doubao |
|---|---|---|
| 图进 `ToolMessage` | ✅ `tool_result` 内容块可含图 | ❌ **协议不允许**,tool 角色只收文本 |
| 图进 `HumanMessage` | ✅ | ✅ 已实现(`_human_content` 解 `image_ref` 块) |

`openai.py:618` 的翻译就是照协议写的:

```python
elif isinstance(msg, ToolMessage):
    out.append({"role": "tool", "tool_call_id": ..., "content": _message_text(msg)})
```

而 `OpenAICompatibleProvider(OpenAIProvider)` —— GLM / Kimi / DeepSeek /
Qwen / Doubao **全部继承这套翻译**。目录里 27 个 `vision=True` 的型号,
实际在跑的 `kimi-k3` / `glm-5.3-flash` / `glm-4.6v` 全在这一侧。

**给 `ToolResult` 开图片通道 = 造一个大部分机队收不到的东西。**
hermes 能那么做是因为它原生打 Anthropic。

我们的等价物是**尾部隐藏 `HumanMessage`**(B-67 / L1 既有通道):两个适配器
都支持,而且 OpenAI 官方对这个场景的建议本来就是「tool 角色装不了图,
改发后续 user 消息」。

另一条约束:`ask_image` **只在**主模型 `supports_vision == False` **且**
manifest 声明了 `vision:` 块时才挂(`agent_factory.py:835`)。它不普遍可用。

### 8.2 三分投递

复用 `_child_run.build_seed_content` 已经确立的判据(`_child_run.py:152-162`):

| agent 能力 | 投递 | 理由 |
|---|---|---|
| `supports_vision` | Path A:页面 ref 作为 **image 内容块**挂在尾部隐藏 `HumanMessage`(§8.5) | 主模型直接看 |
| `can_ask_image` | Path B:figure map 里列出页面 ref,模型调 `ask_image(ref, question)` | 字节从不进主上下文 |
| 都不行 | **只说有图、说明读不了,不给 ref** | 「命名它们只会诱使它编答案」(既有注释) |

### 8.3 渲染页落在哪

**`.tool_results/<run_id>/figures/<doc-sha>/page-NN.jpg`**

`WORKSPACE_OVERFLOW_DIR = ".tool_results"` 这个前缀已经满足全部要求:

- 在 `WORKSPACE_RESERVED_PREFIXES` 里 → browse 界面隐藏,不污染用户产物列表;
- **不在** `WORKSPACE_DELETE_PROTECTED_PREFIXES` 里 → 平台自己能回收,
  session-purge 钩子已经 `rm -rf .tool_results/<run_id>/`;
- NAS 挂在 control-plane 上(`/mnt/workspaces`),读字节不用过沙箱 stdout。

**不开新目录、不开新 TTL、不新增配额来源** —— 原则 4。

### 8.4 ref 形态

新 scheme,**不动** `parse_image_ref`:

```
expert_work://workspace/<tenant>/<user>/.tool_results/<run_id>/figures/<doc-sha>/page-07.jpg
```

- `parse_image_ref` 是明标的系统边界
  (`packages/expert-work-protocol/src/expert_work/protocol/multimodal.py:58`,
  docstring 写着 "This is a system boundary"),往里加分支是把一个已加固的
  校验点重新打开。**加兄弟,不加分支。**
- 新解析器复用 `artifact._validate_path`(`tools/artifact.py:68`)的同一套规则:
  折叠绝对路径、拒 `..`、拒 `agents/` 与 `shared/` 首段。
- `ImageResolver` 是 **Protocol**
  (`services/orchestrator/src/orchestrator/multimodal.py:91`),
  新增一个 NAS 实现是**加实现不是改接口**。
- `AskImageTool.invoke` 按 scheme 分派,租户校验不变(`image_ref.tenant_id != ctx.tenant_id` 那一条对两种 ref 都执行)。

### 8.5 Path A 的完整形态:新标记 + 滑窗 + 可见占位符

#### 为什么要第三个标记

现有两个标记是按**过期语义**分的(`conversation_channel.py:29-45`):

| 标记 | 过期含义 | 规矩 |
|---|---|---|
| `INPUTS_BLOCK_MARK` | 路径失效 | 压缩后把最新一段放回去 |
| `WORKSPACE_BLOCK_MARK` | **内容本身在说谎**(上一轮快照已不是「现在」) | 去重,只留最新 |

**图是第三种:它不过期。** 一份上传文档的第 7 页,渲出来是什么就永远是什么。

- 用去重(工作区那条规矩)是错的 —— 模型看第 7 页时会丢掉还有用的第 3 页;
- 用「放回最新」(输入那条)同理。

所以 `FIGURE_BLOCK_MARK` 自己一条规矩:**累积 + 滑窗**。

#### 滑窗

两家独立撞上同一个数 —— hermes `_MAX_KEEP_TOOL_IMAGES = 3`、
openclaw `keepLastAssistants: 3`。**起手也用 3**(`FIGURE_KEEP_RECENT`)。

#### 退役是替换,不是删除

超窗的图片块换成**可见文字**:

```
[图:第 7 页(用药清单)已退出上下文。需要重看就再调一次 read_page。]
```

hermes(`[image]` / `[image: <url>]`)与 openclaw
(`[image removed during context pruning]`)都是这个形状。

**删除是静默失效,替换不是** —— 模型看得见这里原来有张图,也看得见怎么拿回来。
这一条直接服务于设计原则 1。

#### 退役时机绑在缓存生命周期上

openclaw 的 `mode: "cache-ttl"`(ttl 5 min):裁剪**只在 prompt 缓存已经过期
之后**做,所以裁剪永远不会打掉一个还活着的缓存前缀。这条照抄。

#### 我们比两家省掉的一层

`image_ref_block` 存进历史的是 **URI 字符串**,字节由适配器在调用时解析:

```python
def image_ref_block(uri: str) -> dict[str, str]:
    return {"type": IMAGE_REF_BLOCK_TYPE, "ref": uri}
```

| | hermes / openclaw | 我们 |
|---|---|---|
| 检查点里存什么 | base64 大块 | **一个 URI** |
| 「退役一张图」是什么操作 | 重写多 MB 的消息 | **删一个 ref** |
| 续跑加载成本 | 随图数增长 | 恒定 |

hermes 为此写了 `drop_stale_api_content` / `_strip_images_from_tool_msg`
一整套。**我们不需要。**

### 8.6 ❗ 前置缺陷:估算器看不见图

`packages/expert-work-runtime/src/expert_work/runtime/tokens.py:174` ——
图片块只按它的**字符串表示**计入:

```python
# Tool-use / image / other → coarse repr keeps the ...
```

`{"type":"image_ref","ref":"expert_work://..."}` ≈ 80 字符 ≈ **20 token**,
而真实成本 ≈ **1000–1300 token**。**低估约 65 倍。**

后果:动态裁剪、压缩触发、working window 这些闸门对图片是**瞎的** ——
塞三张图进去,它们以为只加了 60 token。

两家都有这个常量(hermes `agent/image_token_cost.py`,压缩触发器用同一个数;
openclaw `IMAGE_CHAR_ESTIMATE = 8_000`)。

⚠️ **这不是本设计引进的缺陷** —— 今天用户自己传图就已经在低估。但
Path A 会把这个洞放大到必然出事,所以它是 Path A 的**前置**,必须先修。
修法:`image_ref` 块按一个可配的每图 token 常量计,与 §7.1 的渲染分辨率
取同一个真源。

---

## 九、分格式行为

### 9.1 pptx —— P0

| | |
|---|---|
| 触发 | `ppt/media/` 非空 或 任一 slide 有 `PICTURE`/`CHART`/`diagram` |
| 单位 | **整页 slide**,不是单张图(slide↔page 1:1) |
| 清单项 | 页号 + slide 标题 + 形状构成(N 图 / 图表 / SmartArt) + 演讲者备注(有则带,**每页 500 字上限**,见 §14.3) |
| 空页判据 | `chars == 0` 且有图形 → 标 **「本页无任何文字」** |

### 9.2 PDF —— P0

| | |
|---|---|
| 判据 | §5.5 的三重阈值 |
| 清单项 | 连续空页压成区间(hermes 的 `groupby(page - index)`)+ **前文锚点** |
| 锚点 | 往回找最近一个有字的页,取其尾部 60 字符 |

清单形如:

```
  pages 6-9 (4 pages) — after "三、近三个月体重与血糖趋势" (p5)
```

模型必须在**没看见内容**的前提下决定花不花钱。
「缺口前面最后一段文字」正好给了它判断依据。

### 9.3 docx —— P2

| | |
|---|---|
| 实证 | 上传件 **0/20** 带图 |
| 做法 | 与 pptx 同一条路复用,边际成本近零 |
| 锚点 | **比 PDF 更准**:`page.crop((0, 0, width, img.top))` 取图正上方的文字尾部 |

实测输出:

```
p1: FIGURE 288x192pt — after '一、体检概览 这是第一段正文，后面跟着一张血糖趋势图。'
p1: SKIP decorative 29x29pt
```

额外的免费信号:`<wp:docPr descr>` 是 Word 无障碍功能写的 **alt 文字**,
有就直接作为图说明,**连渲染都省了**。

> 不拿 docx 论证工期。它是 pptx 代码路径的顺带产物。

### 9.4 xlsx —— 只报不渲

实证 0/7 带图。但 xlsx 的图表是**纯单元格引用**,实测:

```
series val ref: '体检'!$B$2:$B$4
series cat ref: '体检'!$A$2:$A$4
series name ref: '体检'!B1
```

那些单元格我们**已经抽出来了**。输出一句
「D2 处有折线图,画的是 B2:B4 对 A2:A4」,模型零像素就懂。

**xlsx 不做任何渲染。**

### 9.5 图表通用

`ppt/charts/chart1.xml` / `xl/charts/chart1.xml` 里是真数据。实测:

```
cats = ['7月','8月','9月']   series = {'血糖': [6.1, 5.8, 5.5]}
```

抽成文本,**永不渲染**。约 20 行。

> ⚠️ 两环境 chart XML 均为 0。这是**便宜的正确性保险,不是收益来源**。

---

## 十、输出形状

### 10.1 位置:前置

清单**必须前置在 `content` 头部**。

`read_document` 返回 `text[:cap]` 硬截断(`_DOC_OUTPUT_CHAR_CAP`),
尾部告警会被砍掉。hermes 出于同样的道理(它那边是 `read_file` 分页,
footer 可能永远取不到)也是 PREPEND。

### 10.2 三条文案纪律(抄 hermes)

1. **明确禁止全量** —— 「挑你真正需要的,不要把所有页都渲出来」
2. **给精确的恢复动作** —— 工具名 + 确切参数,模型不用猜
3. **从不点名具体 skill** —— 说「检查是否有可用的 OCR 技能」,不写死名字

### 10.3 条目上限

`MAX_MAP_ENTRIES = 20`,超出折叠成「… 另有 N 处,共 M 页」。
交替出现的图文页会把清单撑爆。

---

## 十一、降级:三态,不是两态

hermes 有一个缺陷**不抄**:`pdftotext` 不在时它返回 `None` 然后**完全不告警** ——
「测不了」和「测了没问题」被合并成同一个输出,退回静默。

本设计强制三态:

| 状态 | 输出 |
|---|---|
| 测了,没发现图 | 不加任何前置块(今天的行为) |
| 测了,发现图 | figure map |
| **测不了** | **明确说「本文档的图片情况无法确定」** + 原因 |

「测不了」的触发:soffice 超时 / 转换失败 / 文档超 `_MAX_DOC_BYTES` /
zip 损坏。**任何一条都不许静默变成「没有图」。**

---

## 十二、明确不做 / 已知缺口

| 项 | 决定 | 理由 |
|---|---|---|
| born-digital PDF 里的矢量图表 | **不做**,进 ROADMAP 记账 | §5.2 —— PDF 侧没有可靠判据,表格误报挡不住 |
| 抽 media blob 直出 | **不做** | §6.1 |
| 抬 `_MAX_DOC_BYTES` | **不做** | 两环境 >25 MB 上传件均为 0 |
| xlsx 渲染 | **不做** | 0/7 |
| pptx 里的 mp4 | **不做** | 全部来自 agent 自己的产出,不是输入侧问题 |
| 托管 OCR(hermes 的 Firecrawl 路线) | **不做** | 外发客户文档,且我们有自己的 VL 通道 |
| 重型解析器(MinerU/Marker/Docling) | **不做** | 全是 GPU 路线;且 docling 在 docx/pptx 图片这块**本来就没做**(issue #2225 open) |
| SmartArt(`diagram` uri) | **进清单,不单独验** | 手上无样本;它走与 picture 相同的整页渲染路径,无专门代码 |

---

## 十三、验收

### 13.1 结构性(必须)

这些是「修好了才成立的不变式」,不是统计:

1. 一份纯图 pptx(`chars == 0`)→ 输出**必须**含 figure map,且**不得**声称文档为空
2. 一份纯表格 docx → figure map **必须为空**(零误报)
3. 一份含 EMF 的 docx → 转换成功,清单里有该图,**不得**出现 Pillow 异常
4. soffice 不可用 → 输出**必须**是「测不了」,**不得**是「没有图」
5. 同名 `x.docx` + `x.pptx` 同时处理 → 两份 PDF 都在(§6.3 坑 1 的回归钉)
6. 22 页文档渲第 11 页 → 拿得到文件(§6.3 坑 2 的回归钉)
7. 估算器:一条带 `image_ref` 块的消息,估出的 token **必须**显著高于
   该块字符串表示的长度(§8.6 的回归钉 —— 今天这条会红)
8. 滑窗:连看 4 页后,第 1 页的图片块**必须**已被替换成占位文字,且占位文字
   里**必须**含恢复指引;该消息**不得**被整条删除
9. 读了文档但一次都没请求渲染 → **soffice 进程数必须为 0**(§4 的「推迟转换」
   回归钉;用 exec 侧的进程计数或耗时上界判)
10. 渲染分辨率:任一页型渲出的图**必须** ≤ 1.15 MP 且长边 ≤ 1568 px(§7.1)

每条都要能 break → red → restore → green 自证。

### 13.2 真栈

用 `ai-health-plan` 的历史输入重放,对照组 = 当前 main:

- 带图 pptx 的 run:实验组**必须**出现 figure map;
- 无图 docx 的 run:两组输出**必须字节一致**(证明零图路径没被碰)。

### 13.3 观察项(不作为通过条件)

- §7.2 的预算起手值(`MAX_PAGES_PER_CALL`、两条驻留护栏)上线后按真实 run 复算;
- **每个 run 实际渲多少页** —— 这是判断要不要补回「单 run 累计上限」的唯一依据
  (§7.2 修订:初稿的 12 页是猜的,已删);
- 装饰图阈值(40 pt / 1 %)同理。

---

## 十四、已定(原为待评审三条)

初稿把三条留给评审;对照两份参考实现的源码 + 去数之后,三条都定了。
记在这里是因为**理由比结论重要** —— 将来要翻案得先推翻这里的依据。

### 14.1 Path A 的落点 → **单开 `FIGURE_BLOCK_MARK`**(§8.5)

理由不是「怕挤」,是**过期语义不同**:现有两个标记一个「路径失效」
一个「内容在说谎」,图这两样都不是 —— 它不过期。套任何一条现成规矩都会
丢掉还有用的旧图。

### 14.2 渲染页跨轮存活 → **随 run 收(§8.3),但缓存清单**

两份参考实现**都不缓存**转换产物:

- hermes `_temp_copy` 用 `NamedTemporaryFile` 且 `finally: os.unlink`;
  视觉侧的 `cache/vision` 名为 cache 实为临时目录,用完 `_unlink_quietly`;
- openclaw 的 `extractionCache` 是 `runPdfPrompt` 的**函数内闭包变量**,
  只为厂商 fallback 重试时不重复抽取,零跨调用。

RAG 流水线全都缓存,因为**索引本身就是产品**;agent 工具都不缓存,因为
文档是在一次任务的语境里读一次。**我们是 agent,不是索引器。**

加上两条已经变了的前提,代价比初稿小得多:

1. 清单来自来源侧 OOXML(~1 ms),**不需要转换**;
2. 转换推迟到**首次渲染请求**(§4)—— 不看图的 run 一秒不花。

**但抄 hermes 一条**:`_describe_image_for_anthropic_fallback` 缓存的是
**文字描述**(按 `sha256(image_url)` 索引),并在文本里附一个指针
——「要细看就用这个 ref 重新取」。同构地,我们缓存 **figure map 文本**
(~1 KB,按文档 sha 索引)并带指针,贵的字节不留。

**上线后量**:同一文档指纹在多少个不同 run 里触发过转换。数大了再加,
`inputs/cache/` 是现成的内容寻址缓存(B-61 已在回收)。

### 14.3 pptx 演讲者备注 → **抽,但设 500 字/页上限**

去数了:测试环境 **1649 张 slide,带备注的 0 张**(uploads 桶 0/39)。

所以今天成本是零。不抽的话,将来真来一份带讲稿的 deck 又是一次静默丢失。
与「图表抽成数据」同类:**便宜的保险,不拿它论证收益**。

---

## 附:参考实现对照

| 维度 | hermes-agent | openclaw | 本设计 |
|---|---|---|---|
| 判据粒度 | 逐页字符 | **全文档**总字符 | **来源侧 OOXML** + PDF 逐页 |
| 部分扫描 | ✅ | ❌ | ✅ |
| 正文正常但含看不见的图 | ❌ | ❌ | ✅ |
| Office 图片 | ❌ 不做 | ❌ 不支持 | ✅ |
| 谁决定看图 | 模型 | 阈值自动 | 模型 |
| 渲染限流 | 页数,每次调用 | **像素预算,每次调用** | 页数,每次调用(无单 run 累计,§7.2 修订) |
| 依赖缺失 | ❌ 静默回退 | ✅ 降级告警 | ✅ 三态 |
| 图表 | ❌ | ❌ | ✅ 抽成数据 |
| 图进历史的通道 | 工具结果(`_multimodal` 封套) | 工具结果(`ImageContent`) | **尾部 HumanMessage**(协议所迫,§8.1) |
| 历史里存什么 | base64 大块 | base64 大块 | **URI ref**,字节调用时解析 |
| 历史滑窗 | ✅ keep 3 + `[image]` 占位 | ✅ keep 3 + `[image removed…]` | ✅ keep 3 + 带恢复指引的占位 |
| 退役时机 | 压缩时 | **缓存 TTL 到期后** | 缓存 TTL 到期后(抄 openclaw) |
| 嵌入预算与看图预算分开 | ✅ 256 KB vs 20 MB | ➖ 单一像素预算 | ✅ §7.2 |
| 估算器认图 | ✅ `image_token_cost` | ✅ `IMAGE_CHAR_ESTIMATE` | ⚠️ **今天不认,Path A 的前置**(§8.6) |
| 转换产物跨会话缓存 | ❌ | ❌ | ❌(缓存清单文本,§14.2) |

docling(业界最认真的开源解析器)在 docx/pptx 图片这块
[issue #2225](https://github.com/docling-project/docling/issues/2225) 仍 open:
docx 走 SimplePipeline,`do_picture_description` 无效果;pptx 没有专门的图片后端;
且只能抽 bitmap,DrawingML / WMF / EMF **不识别且无报错**。

**这块没有可抄的实现。** 但我们比他们有利 —— LibreOffice 与 poppler 已经在
沙箱镜像里,而他们是库,不能假设宿主机有 LibreOffice。
