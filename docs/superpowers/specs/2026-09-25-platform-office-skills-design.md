# 平台 office 技能重写（docx / pptx / xlsx / pdf）设计

> 2026-09-25。读者：实现者、评审、执行生产发布的人。
> 目标上线：**2026-09-28 班车 2 发布窗口**（导入生产是数据操作，不依赖代码版本，见 §7）。

## 0. 背景

### 0.1 起因

测试环境 `ai-health-plan` 配置优化（2026-09-25）时，数了近 30 天 251 个 run：

- 平台技能 `docx` / `pptx` 共被打开 4 次（docx 2 次、pptx 2 次，全部时间窗）。
  每一次模型读完技能后**都没照着做**：技能教的「新建」路线是 JavaScript
  （docx → npm `docx`；pptx → `pptxgenjs`），模型退回预装的 python-docx / python-pptx
  自己完成（run_event 里 `exec_python` 调用逐条可见）。技能正文（docx 19,181 字符、
  pptx 8,344 字符）只是被白读。
- 沙箱里**没有 npm**（B-55 已删；此前实测 832 个 run 里 0 次装成过任何 npm 包，
  出网代理对 npm 回 407）。`sandbox_image_contract.py` 的 `SANDBOX_UNAVAILABLE_NOTE`
  就是为这两个技能写的补丁提示。

### 0.2 许可问题

平台上的 `docx` / `pptx` / `pdf` / `xlsx` 四个技能是 2026-08-03 从 Anthropic
`anthropics/skills` 原样导入的（supporting files 里带着原版 `LICENSE.txt`）。
原版许可证原文（节选）：

> users may not: … Reproduce or copy these materials … **Create derivative works
> based on these materials** … **Distribute, sublicense, or transfer these materials
> to any third party** …

- 在原版上改写 = 衍生作品，**不可行**。所以本设计是**从零自写**（clean-room）：
  不复制原版的正文文字与脚本代码，只依据我们自己的沙箱环境与公开库文档编写。
- 这与 2026-06-05 `docs/streams/STREAM-OFFICE-DESIGN.md` OFFICE-ADR-5 的判断一致
  （「Anthropic 官方 docx/xlsx/pptx/pdf 是 Proprietary 明文禁移植」），ADR-5 当时留的路正是
  「包来源由管理员定（自打 / 挑 license 干净的现成包）」。本设计 = 自打。
- 这四个技能目前绑定在测试环境对接方租户的 3 个 Agent 上（`ai-health-plan` /
  `ai-health-report` / `sop2-designer`；`xlsx` 另有 `test-agent`）。

### 0.3 目标

1. 四个技能在**我们的沙箱**里照着做就能成功：零 JS 路线，只用预装的 Python 库与命令行工具。
2. 正文短而准（每份目标 2,000~4,000 字符），容易出错的操作由**经过测试的脚本**承担。
3. 质量有机器守门：技能里写到的每条命令，都在 CI 里用真实沙箱镜像跑过。
4. 同名替换：已绑定的 Agent 不改配置，自动用上新版。

## 1. 已定决策（2026-09-25 用户拍板）

| # | 决策 | 备注 |
|---|---|---|
| D1 | 四个一起重写：docx / pptx / xlsx / pdf | |
| D2 | 场景：①新建 ②检查成品 ③格式转换 必做；④编辑已有文件 做精简版；⑤读取 不写（平台 `read_document` / `read_page` 已覆盖）；⑥高级功能不做 | 「精简版编辑」定义见 §2 |
| D3 | 源码放本仓库 `platform-skills/`，走 PR + CI | 推翻 ADR-5「平台不预置内容」一句，见 §9 |
| D4 | **保留**旧版本（Anthropic 原版 = 第 1 版）在版本历史里，不删 | 许可风险仍在，用户知情选择，见 §8 |
| D5 | 组织方式：简短正文 + 经过测试的小脚本 | 版面内容仍由模型自己写代码 |
| D6 | 共享脚本**按技能显式声明**分发，不声明不给 | 为以后非 office 技能留干净边界 |
| D7 | 赶 2026-09-28 上线，**不因赶时间降低标准**；来不及的明说，逐技能独立决定 | 见 §7.4 |
| D8 | 正文中文，代码 / 库名 / 命令保持英文 | |
| D9 | 字体可配：Word / PPT 默认写「微软雅黑」（照顾客户电脑），PDF 用沙箱里的开源字体并嵌入；员工指定字体时按 §4.6 处理；品牌字体要求高时建议交付 PDF | 见 §4.6；往沙箱加开源字体另立 backlog（§9） |

## 2. 范围

| 场景 | docx | pptx | xlsx | pdf |
|---|---|---|---|---|
| ① 新建 | python-docx | python-pptx | openpyxl | weasyprint（HTML → PDF） |
| ② 检查成品 | 重新打开 + `preview.py` + 看图 | 同左 | 同左 + `recalc.py` 报错单元格 | 同左 |
| ③ 格式转换 | `convert.py`（→ pdf；.doc → .docx） | `convert.py`（→ pdf；.ppt → .pptx） | `convert.py`（→ pdf；.xls → .xlsx） | —（PDF 是转换终点） |
| ④ 精简编辑 | 保留格式替换文字、`{{占位符}}` 模板填充、表格填值、增删段落 / 图片、页眉页脚 | 模板选版式填占位符、删页调序、换图、复制一页 | 保留样式与公式改单元格、加行加表 | 合并 / 拆分 / 旋转 / 文字水印 / 叠加盖章页 |

**不做**（技能里写明「做不到，如实告知用户」）：修订痕迹、批注、改内嵌图表 / SmartArt 数据、
动画、数据透视表、宏、PDF 表单填写 / 加密 / OCR、**改 PDF 里的文字**（回到源文件改后重新生成）。

**编辑的两条硬规则**（四个技能都写）：
1. 输出到**新文件**，绝不覆盖原件；脚本层面强制（输入输出同路径直接报错退出）。
2. 改完必须走「检查成品」。

## 3. 仓库结构与打包

```
platform-skills/
  README.md                  ← 这个目录是什么、怎么改、怎么发布
  shared/                    ← 共享脚本库：只存放，不自动分发
    preview.py
    convert.py
    _office.py               ← preview/convert 共用的 soffice 调用封装（同样按声明分发）
  docx/
    SKILL.md
    skill.yaml               ← shared: [preview.py, convert.py, _office.py]
    scripts/replace_text.py
    scripts/fill_template.py
  pptx/
    SKILL.md
    skill.yaml               ← shared: [preview.py, convert.py, _office.py]
    scripts/inspect_template.py
    scripts/duplicate_slide.py
  xlsx/
    SKILL.md
    skill.yaml               ← shared: [preview.py, convert.py, _office.py]
    scripts/recalc.py
  pdf/
    SKILL.md
    skill.yaml               ← shared: [preview.py]
    scripts/pdf_ops.py
  build.py                   ← 打包
  tests/                     ← 第一层、第二层测试（§6）
```

### 3.1 `skill.yaml`

```yaml
shared:            # 从 platform-skills/shared/ 复制进本技能 scripts/ 的文件；缺省 = 空
  - preview.py
  - convert.py
  - _office.py
```

只有这一个键。未知键、列出的文件在 `shared/` 里不存在 → `build.py` 报错退出。

### 3.2 `SKILL.md` frontmatter

```yaml
---
name: docx
description: 新建、修改或转换 Word 文档（.docx）时使用：方案、报告、合同、按机构模板出文档、转 PDF。读取文档内容用 read_document，不用本技能。
license: 深护智康自研，仅限本平台使用
expert_work:
  lazy: true
  category: 通用
---
```

- `name` 与现有平台技能同名（`docx` / `pptx` / `xlsx` / `pdf`），导入即成为该技能的新版本。
- `description` 中文、写清触发场景与**不该用的场景**（技能目录里只显示这一句，模型据此决定是否打开）。
- `lazy: true`：目录里只放摘要，正文经 `skill_view` 读取（平台默认值也是 true，显式写出免歧义）。

### 3.3 `build.py`

`python platform-skills/build.py [--out DIR] [--only docx,pptx]`

1. 对每个技能目录：读 `skill.yaml`，把声明的共享文件复制到包内 `scripts/`；
   **技能自带脚本与共享脚本同名 → 报错**（不允许静默覆盖）。
2. 生成 `<name>.skill`（ZIP，根目录即 `SKILL.md` + `scripts/`，符合 `_skill_zip.py` 的 U-14 标准布局）。
3. 产物确定性：固定文件顺序与 ZIP 时间戳，同一份源码打出的包逐字节相同
   → 平台导入的 content_hash 幂等才有意义（内容没变的技能重导入返回 200 `created:false`，不产生新版本）。
4. 打印每个包的文件清单与大小。

## 4. 各技能内容

### 4.1 正文统一骨架

每份 `SKILL.md` 按同一顺序：

1. **何时用 / 何时不用**（读取走 `read_document`；不做的事列在「做不到」）
2. **环境事实**（§5，每份都写，写法统一）
3. **新建**：推荐库、最小可用骨架代码、中文与版式要点
4. **修改已有文件**：对应脚本用法 + 硬规则
5. **转换**
6. **检查成品**（固定三步，§4.7）
7. **常见坑**（每条写「现象 → 原因 → 做法」）
8. **做不到的事**（如实告知用户的话术）

### 4.2 docx

- 新建要点：
  - 中文字体必须同时设 `w:eastAsia`（只设 `font.name` 中文会回落成默认字体）；给出设置函数，字体名取 §4.6 的规则。
  - 用样式（Normal / Heading 1~3）而不是逐段手设字号，保证层级统一。
  - 表格：表头行、列宽、单元格内边距、跨页重复表头。
  - 页眉页脚与页码字段、分节、横向页。
  - 插图：按页面可用宽度等比缩放。
- `scripts/replace_text.py`
  - `replace_text.py IN.docx OUT.docx --rules RULES.json`
  - `RULES.json` = `[{"find": "...", "replace": "..."}]`
  - 覆盖正文段落、表格单元格（含嵌套）、页眉页脚。
  - 处理「一句话被拆成多个 run」：在段落内拼接各 run 文本定位，替换后结果写入命中的第一个 run，
    其余被覆盖的 run 清空，**保留第一个 run 的格式**。
  - 输出 JSON：每条规则的命中次数；有规则 0 命中时退出码仍为 0，但在输出里标出（让模型自己判断）。
- `scripts/fill_template.py`
  - `fill_template.py TEMPLATE.docx --list` → 列出模板里所有 `{{key}}` 占位符（JSON）。
  - `fill_template.py TEMPLATE.docx OUT.docx --data DATA.json` → 填值；输出「未提供值的占位符」与「多余的数据键」。
  - 占位符只做标量替换（不做循环 / 条件）；表格多行数据按正文里的 python-docx 表格写法做。

### 4.3 pptx

- 新建要点：
  - 16:9（13.333 × 7.5 英寸）、统一页边距与网格；四级文字层级。
  - 中文字体同样要设东亚字体（`a:ea`）；给出设置函数，字体名取 §4.6 的规则。
  - 图表：matplotlib 出 PNG 再插入（不用 python-pptx 原生图表，避免渲染差异）。
  - 视频：`add_movie`（带封面图）。
  - 文字溢出：按内容量拆页，不靠缩字号硬塞。
- `scripts/inspect_template.py`
  - `inspect_template.py DECK.pptx` → JSON：每个版式（layout）的名字与占位符（idx / 类型 / 名字 / 位置），
    每页幻灯片用的版式与已有形状概况。套模板前先看它。
- `scripts/duplicate_slide.py`
  - `duplicate_slide.py IN.pptx OUT.pptx --index N [--after M]`（页码从 1 开始）
  - 复制该页全部形状，并**复制图片 / 媒体关系**（新页引用自己的 rel，而不是原页的）。
  - 限制写在输出和正文里：图表 / SmartArt / 嵌入对象的数据部件与原页共享，改其一会影响另一页。
- 删页、调序在正文里给 python-pptx 写法（操作 `sldIdLst`）。

### 4.4 xlsx

- 新建要点：
  - 表头样式、列宽、冻结首行、数字 / 日期 / 百分比格式。
  - **公式写成公式**（`=SUM(B2:B9)`），不在 Python 里算好写死结果。
  - 条件格式、数据校验的基本写法；图表同样推荐 matplotlib 出图插入。
- 修改要点：`openpyxl.load_workbook` 默认保留公式、样式、图片与原生图表（沙箱镜像 openpyxl 3.1.5 实测：
  加载即有 `_charts` / `_images`，保存后图表与图片部件都在）；真正丢的是**文本框与形状**（保存后消失），
  原生图表经 openpyxl 重写、部分样式可能走样 → 正文要求：照常改，在回复里说明；用户要求原样保留时如实说做不到
  （2026-09-26 终审修正：原稿写「图表 / 图片保存后会丢失」并要求先征得同意，与实测不符，且确认回合会卡住自动运行）。
- `scripts/recalc.py`
  - `recalc.py IN.xlsx OUT.xlsx [--timeout 120]`
  - 用 LibreOffice 重算所有公式并另存（openpyxl 写入的公式没有缓存值，不重算的话别的软件读到的是空）。
  - 重算后扫描错误值（`#REF!` `#DIV/0!` `#VALUE!` `#NAME?` `#N/A` `#NUM!` `#NULL!`），
    输出 JSON：`{"formulas": N, "errors": [{"sheet","cell","value"}]}`；有错误时退出码 3
    （2026-09-26 终审改：原定 2 与 argparse 用法错误的 2 撞车，只看退出码分不开）。

### 4.5 pdf

- 新建要点：
  - HTML + 内嵌 CSS → weasyprint。`@page` 设纸张、边距、页眉页脚（`@top-center` 等）、页码（`counter(page)`）。
  - 中文字体用沙箱里实有的开源字体（默认 `"Noto Sans CJK SC"`），weasyprint 会嵌入 PDF；规则见 §4.6。
  - 分页控制：`page-break-inside: avoid`（表格行、卡片）、`break-before`。
  - 图表：matplotlib 出 PNG / SVG 引用。
- `scripts/pdf_ops.py`（子命令，输出一律为新文件）
  - `info IN.pdf` → 页数、页面尺寸、是否加密、有无文字层。
  - `merge OUT.pdf IN1.pdf IN2.pdf ...`
  - `split IN.pdf OUT_DIR --pages 1-3,5`（不给 `--pages` 则每页一个文件）
  - `rotate IN.pdf OUT.pdf --pages 2,4 --degrees 90`
  - `watermark IN.pdf OUT.pdf --text "内部资料" [--opacity 0.15] [--angle 45]`：
    水印页用 weasyprint 生成（保证中文字体），再逐页叠加。
  - `stamp IN.pdf OUT.pdf --stamp STAMP.pdf [--pages ...]`：把一页 PDF（如盖章页、签名图做成的页）叠加到指定页。
- 读取 PDF 文字 / 表格：正文写一句「用 read_document；需要精确表格结构时 pdfplumber」。

### 4.6 字体规则（D9，四份正文按各自格式写）

事实：Word / PPT 文件里只记**字体名**，客户打开时用的是他自己电脑上的字体，没有就被 Office 替换；
PDF 把字体**嵌入文件**，在任何电脑上都一样。沙箱里的字体只决定 PDF 成品与沙箱内预览图。

| 成品 | 默认 | 员工指定了某个字体 |
|---|---|---|
| Word / PPT | 中文 `微软雅黑`，西文 `Arial`（Windows / Mac 版 Office 普遍自带） | 照写该字体名；回复里提醒一句「客户电脑没装这个字体时，Office 会自动替换成别的字体」 |
| PDF | 沙箱里的 `Noto Sans CJK SC`，嵌入文件 | 沙箱里有（`fc-list :lang=zh` 能查到）就用；没有就如实告知「沙箱里没有该字体，已用 Noto Sans CJK 代替」，**不尝试下载安装字体** |

- 品牌字体要求严格（机构 VI 指定字体、要求客户看到的效果与设计完全一致）时，正文建议交付 PDF。
- 沙箱里没有微软雅黑：预览时 LibreOffice 按字体配置回落到 Noto Sans CJK，版式基本一致；
  正文写明「预览里的字形与客户电脑上可能略有差别，查的是有没有坏、乱，不是像素级还原」。
- 字体名在骨架代码里集中成一个常量（`CN_FONT` / `EN_FONT`），模型改一处即全局生效。
- 第二层测试加一例：用 `微软雅黑` 生成的 docx / pptx 经 `preview.py` 出图，中文正常显示（不是方块），证明回落生效。

### 4.7 共享脚本

- `_office.py`：soffice 调用的唯一实现。
  - 每次调用使用**独立的 LibreOffice 用户配置目录**（`-env:UserInstallation=file://<临时目录>`），
    避免并发转换互相锁住；调用结束删除。
  - `--headless --norestore --nolockcheck`；子进程放进独立进程组，**超时整组杀掉**（默认 120s），
    并以明确的错误信息退出（「转换超时 120s：文件可能过大或损坏」）。
  - 临时目录建在 `tempfile.gettempdir()` 下，不写根文件系统。
- `convert.py`
  - `convert.py IN --to pdf|docx|pptx|xlsx [--out-dir DIR] [--timeout 120]` → 打印输出文件路径。
  - 同格式、不支持的方向 → 明确报错。
- `preview.py`
  - `preview.py IN [--out-dir DIR] [--pages 1-3] [--dpi 110]` → 打印 JSON：生成的 PNG 路径列表与总页数。
  - PDF 直接 `pdftoppm`；docx / pptx / xlsx 及老格式先经 `_office.py` 转 PDF 再转图。
  - 默认只出前 3 页（控制耗时与看图次数），`--pages all` 出全部。
- **「检查成品」固定三步**（四份正文一字不差）：
  1. 用对应库重新打开成品，确认没坏（给出一行校验代码）。
  2. `preview.py` 出图。
  3. 用 `ask_image(path=...)` 看首页和信息最密的一页：中文是否方块、文字是否溢出 / 重叠、版式是否错乱。
     Agent 没配看图能力时跳过这一步，并在回复里写明「未做视觉检查」。

## 5. 平台环境事实（四份正文统一写法）

- 已预装、**直接用、不要装**：python-docx、python-pptx、openpyxl、pypdf、pdfplumber、pdf2image、
  weasyprint、matplotlib、Pillow、pandas；命令行 `soffice`（LibreOffice，writer / calc / impress 三个组件都在，镜像 Dockerfile 装的是三个 `-nogui` 包）、`pdftoppm`；字体 Noto Sans CJK。
- 需要别的 Python 包时可以 `pip install`（走阿里云镜像源，秒级）；但沙箱空闲后会回收、装过的包会丢，
  所以**本技能不依赖任何现装的包**。
- **没有 npm**（有 node）：不要走任何 JavaScript 路线。
- 工作目录 `/workspace`；本技能的脚本在 `$EXPERT_WORK_SKILLS_DIR/<技能名>/scripts/`。
- 成品必须 `save_artifact` 登记，否则用户拿不到。
- 读取已有文档内容：`read_document` / `read_page`，不在本技能里重复。

事实来源：`infra/sandbox-image/requirements.txt`、`services/orchestrator/src/orchestrator/tools/sandbox_image_contract.py`、
`infra/k8s/overlays/*/configmap-patch.yaml`（`EXPERT_WORK_SANDBOX_PIP_*`）。第一层测试把「正文里声称预装的库」与
`SANDBOX_PREINSTALLED_PYTHON` 逐项比对，防止两边漂移（§6.1）。

## 6. 质量门（三层测试）

### 6.1 第一层：单元测试（普通 pytest，每个 PR 跑，秒级）

- 打包：每个 `skill.yaml` 只含 `shared` 键；声明的共享文件存在；技能脚本与共享脚本不重名；
  `build.py` 两次打包逐字节相同。
- 引用完整：正文里出现的每个 `scripts/xxx.py` 在该技能包内存在；包内每个脚本都在正文里被提到（无死文件）。
- **平台导入同款检查**：把打出的包喂给平台自己的 `parse_skill_zip` + `moderate_prompt_fragment`
  + `scan_for_threats(scope="strict")`（正文与每个文本文件）——即 `_ingest_platform_skill_payload` 的
  校验段；再对每个文本文件跑 `scan_for_threats(scope="context")`（`skill_seed.build_skill_seed_files`
  在放进沙箱时用的那道，**命中会被静默丢弃**）。任何一处命中即红。
- 路线约束：正文与脚本里不出现 `npm`、`require(`、`pptxgenjs`、`docx-js` 等 JS 路线字样。
- 环境事实一致：正文列出的预装库 ⊆ `SANDBOX_PREINSTALLED_PYTHON`；列出的命令行工具 ⊆ `SANDBOX_PREINSTALLED_BINARIES`。
- frontmatter：`name` 与目录名一致、`lazy: true`、`description` 非空且 ≤ 200 字符（平台没有单条上限，只有全部技能摘要合计 18,000 字符的索引预算 `MAX_SKILLS_INDEX_CHARS`，超了整个目录退化成只列名字；单条设 200 是我们自己的约束，给同一 Agent 上的其它技能留余量）。
- 正文长度：每份 ≤ 6,000 字符（目标 2,000~4,000；超过即红，逼着删）。

### 6.2 第二层：真实沙箱镜像里跑脚本

- 触发：改动 `platform-skills/**` 或 `infra/sandbox-image/**`（镜像变更也要验，删库或删 soffice 会当场红）。
- 镜像：与线上同一份 `infra/sandbox-image/Dockerfile`，复用现有 CI 的构建缓存。
- 运行条件贴近线上：只读根文件系统，`/workspace`、`/tmp`、`/home/agent` 可写，非 root 用户，
  **不通网**（技能必须离线可用）。
- 用例（每条都是断言，不是「跑通即可」）：
  1. 各格式新建一份含中文标题、表格、插图的样例（用正文里给出的骨架代码原样执行）。
  2. `preview.py`：四种格式都产出 PNG，页数与预期一致；PNG 非空白（像素方差阈值）。
  3. PDF 中文：weasyprint 产物里嵌入了 CJK 字体（pypdf 读字体表）。
  4. `convert.py`：docx / pptx / xlsx → pdf 成功；**超时用例**（给一个 1s 超时）确实中断且进程组被清理、
     退出码非 0；两个转换并发执行都成功（独立配置目录生效）。
  5. `replace_text.py`：构造「一句话拆成三个 run、第一个 run 加粗」的文档，替换后文字正确且仍加粗；
     表格与页眉里的目标也被替换；输入输出同路径被拒。
  6. `fill_template.py`：`--list` 列全占位符；缺值与多余键被报告。
  7. `inspect_template.py` 能列出版式与占位符；`duplicate_slide.py` 复制带图片的页后文件能被 python-pptx
     与 LibreOffice 打开，新页图片独立存在。
  8. `recalc.py`：含 `=1/0` 的表报出 `#DIV/0!`、退出码 3；正常表退出码 0、公式有缓存值。
  9. `pdf_ops.py`：merge / split / rotate / watermark（中文水印）/ stamp 各一例，页数与旋转角度断言。
- 实现形式：一个 pytest 文件在宿主机驱动 `docker run`（仿照现有 `infra/sandbox-image/smoke_test.py`
  的做法），挂上打好的包；具体接入哪个 workflow（新 job 还是并入 `sandbox-image.yml`）在实施计划里定。

### 6.3 第三层：测试环境真实验收（导入测试环境后，人工执行，记录结果）

- 临时探针 Agent（对接方租户，glm-5.3 主模型，配看图模型，绑定四个技能），逐项：
  1. 新建：中文 Word 报告、PPT、Excel（带公式）、PDF 各一份。
  2. 上传一份带 `{{占位符}}` 的 Word 模板，让它按模板出文档。
  3. 上传一份 PPT，让它复制第 2 页并改文字。
  4. 把 Word 转成 PDF。
  5. 让它「给这份 PDF 加上内部资料水印」。
- 每项判据：
  - 模型**确实打开了对应技能**（run_event 里有 `skill_view`），且运行了技能脚本（`$EXPERT_WORK_SKILLS_DIR/...`）；
  - 没有任何 npm / JS 尝试；
  - 成品 `save_artifact` 登记，下载后能打开，`preview.py` 出图人工目检中文与版式正常；
  - run `completed=true`。
- 回归：`ai-health-plan` 用真实场景出一次方案，与本次改动前的同类 run 对比（成功与否、耗时、工具失败次数）。
- 探针 Agent 与测试数据用完删除。

## 7. 上线

### 7.1 导入脚本

`platform-skills/import_in_pod.py`：从 stdin 读入 `.skill` 包，在 control-plane pod 内调用平台导入接口背后的
**同一段处理代码**（`_ingest_platform_skill_payload`：解析 → 名字校验 → 审核 → 严格扫描 → 幂等建版本 → 审计），
以平台系统身份执行，审计里 `source` 标明来自本脚本。输出每个技能：`created` / `already present`、新版本号、content_hash。
执行方式与既有 runbook 一致：本地 `kubectl exec -i <pod> -- python3 - < 脚本`，包的字节随 stdin 传入，
**不需要脚本提前进镜像**。控制台「平台技能」页面上传 `.skill` 包是手工备用路径，效果相同。

实施计划第一步核实：`_ingest_platform_skill_payload` 需要的 `Principal` / `AuditLogger` / 对象存储参数在 pod 内如何构造。

### 7.2 生效时机

- 平台对构建好的 Agent 有缓存（按 manifest 指纹），导入端点只在返回 201 时清缓存，而且只清处理这次请求的那个副本。
- **测试环境**：导入后我执行 `kubectl rollout restart deploy/control-plane`，确保两个副本都用新版；
  实施计划里同时核实是否存在跨副本失效机制，若存在则省去重启（结论写回本节）。
- **生产（09-28）**：导入放在班车 2 执行单 **Step A（新沙箱镜像）之后、Step B（发版）之前**：
  - 必须在 Step A 之后：技能脚本依赖新沙箱镜像里的 LibreOffice 与各库，第二层测试验的也是这份镜像；
  - 在 Step B 之前：Step B 的滚动发布会重启所有 control-plane，缓存自然清空，无需额外重启。
- 导入前后各做一次只读核对（写进执行单）：生产里这四个平台技能的 `latest_version` 与绑定它们的 Agent 列表。

### 7.3 回滚

- 新版本有问题：从本仓库 git 历史取上一版源码 → `build.py` → 导入（平台存成新版本，内容同旧版）。
- 需要紧急退回 Anthropic 原版：旧版本保留在历史里（D4），控制台导出第 1 版再导入。
- 每个技能独立回滚，互不影响。

### 7.4 时间表与逐技能决策

| 时间 | 事项 | 完成判据 |
|---|---|---|
| 09-25 | 本设计 + 实施计划 | 用户审过 |
| 09-26 | 实现四个技能与脚本；第一、二层测试 | CI 绿、PR 合并 |
| 09-27 | 导入测试环境；第三层验收；问题当天修 | 每个技能一份验收记录 |
| 09-27 晚 | **逐技能 go / no-go**，结论写进执行单 | 未通过的技能不导入生产，保持原版 |
| 09-28 | 班车 2 窗口内导入生产（§7.2 位置） | 导入输出 + 发布后金丝雀 / 一次真实 run 验证 |

任一环节判断来不及，**当时就告知用户差多少**，由用户决定延期还是缩范围；不自行降低 §6 的任何一层。

## 8. 风险与已知限制

| 风险 / 限制 | 处置 |
|---|---|
| Anthropic 原版仍作为第 1 版留在版本历史，管理员可查看 / 导出（D4，用户知情选择） | ROADMAP 登记；日后若要彻底清除，删除旧版本前须先核实引用（agent 固定版本号、使用记录外键） |
| 模型不打开技能（ai-health-plan 的提示词已包办全流程） | 技能本身不解决；ai-health-plan 的提示词优化单独进行，那边可加「生成 Word / PPT / PDF 前先读对应技能」 |
| LibreOffice 渲染与 Microsoft Office 有差异，预览图 ≠ 用户电脑上的效果 | 正文写明预览用途是查「坏没坏、乱没乱」，不是像素级还原 |
| 成品在客户电脑上的中文字体（Word / PPT 只记字体名） | 已定 D9：默认微软雅黑 + 指定字体时提醒；严格场景交付 PDF（§4.6） |
| openpyxl 改已有 xlsx 会丢原有图表 | 正文要求先告知用户 |
| soffice 冷启动慢（首次转换数秒） | 超时默认 120s；正文提示合并多次转换 |
| 时间紧 | §7.4 逐技能 go / no-go，不降标准 |

## 9. 文档联动

- `docs/streams/STREAM-OFFICE-DESIGN.md`：ADR-5 追加 2026-09-25 修订——平台自写 office 技能，源码在
  `platform-skills/`，理由（原版许可禁衍生、原版新建路线依赖 npm 而沙箱没有）；同时更正 §0.1 / ADR-1
  已过时的「运行时卸载 pip」「libreoffice 推后」两处（B-55 / B-81 之后均已不成立）。
- `sandbox_image_contract.py` 的 `SANDBOX_UNAVAILABLE_NOTE`：新技能上线后，其中「docx/pptx 技能写着
  npm」的说明改为一般性表述（实施计划里处理，避免对已不存在的正文做注释）。
- `docs/runbooks/2026-09-24-prod-release-checklist.md`：新增「导入 office 技能」步骤（§7.2 位置）与回滚条目。
- `docs/superpowers/ROADMAP.md`：登记本项；登记旧版本许可风险（D4）；新立 backlog「沙箱镜像加开源中文字体」——
  由用户先定风格（宋体类 / 楷体类 / 圆体类等），逐个核许可证是否允许打包再分发（微软雅黑、宋体、黑体等商业字体不可），
  随节后沙箱镜像更新上；不走运行时 pip / 下载（PyPI 镜像源没有字体，出网要放白名单，沙箱回收即丢）。

## 10. 完成标准

1. 四个技能的第一、二层测试在 CI 全绿并合入 main。
2. 测试环境导入后，第三层验收每项有记录且通过（未通过的技能按 §7.4 不上生产）。
3. `ai-health-plan` 回归无退步。
4. 生产导入完成，导入输出与发布后验证记录在班车 2 执行单 §6。
5. §9 的文档全部更新。
