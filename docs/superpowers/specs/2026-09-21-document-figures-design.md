# 文档里的图:从静默丢失到按需取用(B-64)

**状态**:设计定稿待评审
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
             ④ pdftoppm -jpeg -r 150 -f N -l N   (0.16 s / 173 KB)
                    │
                    ▼
             ⑤ 按 agent 能力三分投递(§8.2)
```

**阶段 ①②③ 在一次 `read_document` 调用里完成;④⑤ 是模型的第二次工具调用。**

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

**默认 `-jpeg -r 150`。** JPEG 比 PNG 小 6.5×。150 dpi 下一页 1500×1125 px,
按 `w×h/750` 约 2250 图像 token,与 Anthropic 官方「1500–3000 tok/页」一致。

### 7.2 预算(原则 3 的落地)

抄 openclaw 的**像素预算**形状,而不是页数上限:

- 单次 `read_page` 调用:`MAX_PAGES_PER_CALL`(起手 3)
- 单 run 累计:`MAX_RENDER_PIXELS`(起手 12 M px ≈ 7 页 @150 dpi)
- **塞不下时降分辨率,不丢页** —— `resolveRenderPlan` 那套二分找最大可行 scale
- 预算耗尽 → 工具返回明确的「预算用完,已渲 N 页」而不是静默截断

起手值是**猜的**,进验收观察项(§13.3)。

---

## 八、投递

### 8.1 约束

- `ToolResult.content` 是 **`str`** —— 工具结果**回不了图片块**。
- `ask_image` **只在**主模型 `supports_vision == False` **且** manifest 声明了
  `vision:` 块时才挂(`agent_factory.py:835`)。它不是普遍可用的。

所以投递必须按 agent 能力分支,而不是假设某一条通道总在。

### 8.2 三分投递

复用 `_child_run.build_seed_content` 已经确立的判据(`_child_run.py:152-162`):

| agent 能力 | 投递 | 理由 |
|---|---|---|
| `supports_vision` | Path A:把页面 ref 作为 **image 内容块**挂在本轮尾部隐藏 `HumanMessage` 上(B-67 / L1 既有通道) | 主模型直接看。不进 system,不破坏 prompt 缓存前缀 |
| `can_ask_image` | Path B:figure map 里列出页面 ref,模型调 `ask_image(ref, question)` | 字节从不进主上下文 —— 成本论证的核心 |
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

---

## 九、分格式行为

### 9.1 pptx —— P0

| | |
|---|---|
| 触发 | `ppt/media/` 非空 或 任一 slide 有 `PICTURE`/`CHART`/`diagram` |
| 单位 | **整页 slide**,不是单张图(slide↔page 1:1) |
| 清单项 | 页号 + slide 标题 + 形状构成(N 图 / 图表 / SmartArt) + 演讲者备注(有则带) |
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

每条都要能 break → red → restore → green 自证。

### 13.2 真栈

用 `ai-health-plan` 的历史输入重放,对照组 = 当前 main:

- 带图 pptx 的 run:实验组**必须**出现 figure map;
- 无图 docx 的 run:两组输出**必须字节一致**(证明零图路径没被碰)。

### 13.3 观察项(不作为通过条件)

- §7.2 的三个预算起手值是猜的,上线后按真实 run 复算;
- 装饰图阈值(40 pt / 1 %)同理。

---

## 十四、未决(留给评审)

1. **Path A 的落点。** §8.2 说把 image 块挂在本轮尾部隐藏 `HumanMessage` 上。
   这复用 B-67 的既有通道,但那个通道今天承载的是「本轮输入」,
   把渲染页也塞进去是否要单开一个 mark?
2. **渲染页跨轮存活多久。** 落在 `.tool_results/<run_id>/` 意味着随 run 结束被收。
   同一个 run 内多轮复用没问题;跨 run 重看同一页要重渲(0.16 s,可接受)。
   这个取舍是否确认?
3. **pptx 演讲者备注是否默认带上。** 它常含讲稿正文,信息密度高,
   但也会显著拉长 `content`。

---

## 附:参考实现对照

| 维度 | hermes-agent | openclaw | 本设计 |
|---|---|---|---|
| 判据粒度 | 逐页字符 | **全文档**总字符 | **来源侧 OOXML** + PDF 逐页 |
| 部分扫描 | ✅ | ❌ | ✅ |
| 正文正常但含看不见的图 | ❌ | ❌ | ✅ |
| Office 图片 | ❌ 不做 | ❌ 不支持 | ✅ |
| 谁决定看图 | 模型 | 阈值自动 | 模型 |
| 限流单位 | 页数 | **像素预算** | 像素预算 |
| 依赖缺失 | ❌ 静默回退 | ✅ 降级告警 | ✅ 三态 |
| 图表 | ❌ | ❌ | ✅ 抽成数据 |

docling(业界最认真的开源解析器)在 docx/pptx 图片这块
[issue #2225](https://github.com/docling-project/docling/issues/2225) 仍 open:
docx 走 SimplePipeline,`do_picture_description` 无效果;pptx 没有专门的图片后端;
且只能抽 bitmap,DrawingML / WMF / EMF **不识别且无报错**。

**这块没有可抄的实现。** 但我们比他们有利 —— LibreOffice 与 poppler 已经在
沙箱镜像里,而他们是库,不能假设宿主机有 LibreOffice。
