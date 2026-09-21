# 文档里的图:从静默丢失到按需取用 —— 实现计划(B-64)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 `read_document` 在文档含图时明确告知模型,并给它一条按需取用单页像素的路 —— 消灭「文字照常抽出,图里的信息静默丢失」。

**Architecture:** 三段。① 清单来自来源侧 OOXML(读 zip 里的 XML,~1 ms,不跑 LibreOffice);② 模型挑中某页时才 `soffice --convert-to pdf` 归一化并 `pdftoppm` 渲单页;③ 渲染物按 agent 能力三分投递 —— 视觉主模型走尾部隐藏 `HumanMessage`(Path A),文本主模型走 `ask_image`(Path B),两者都不行就只告知。

**Tech Stack:** Python 3.12 / 沙箱内 `python-docx` `python-pptx` `openpyxl` `pdfplumber` / 沙箱二进制 `soffice` `pdftoppm` `pdfinfo` / LangChain `BaseMessage` / LangGraph `AgentState`

**Spec:** `docs/superpowers/specs/2026-09-21-document-figures-design.md`

---

## Global Constraints

逐条抄自 spec,数值不许改:

- **零新增依赖。** 沙箱镜像已有 `soffice` / `libreoffice` / `pdftoppm` / `pdftotext` / `pdfinfo`、`python-docx 1.2.0` / `python-pptx 1.0.2` / `openpyxl 3.1.5` / `pdfplumber 0.11.10` / `pdf2image` / `Pillow 12.3.0`。**不许 pip install 任何东西**,不许改 `infra/sandbox-image/requirements.txt`。
- **判定只看来源侧 OOXML,不看 PDF 绘图算子。** `pdfplumber.Page.images` 对矢量图形(EMF/WMF/原生图表)一律为 0,拿它判「有没有图」是错的。
- **装饰图按显示尺寸过滤:短边 < 40 pt 或 占页面积 < 1 %。** 不许按原始像素判。
- **渲染 `-jpeg -r 100`。** 不许用 150 —— 四种页型全超 Anthropic 的 1.15 MP / 1568 px 线。
- **soffice 每次转换用自己的 outdir。** 输出名按 basename 派生,同目录会静默覆盖。
- **pdftoppm 输出页号按总页数补零**(22 页文档出 `page-01.png`)。**必须 glob,不许拼文件名。**
- **三态,不是两态。** 「测了没图」/「测了有图」/「**测不了**」必须是三种不同输出。任何失败都不许静默变成「没有图」。
- **清单前置在 `content` 头部。** `read_document` 返回 `text[:cap]` 硬截断,尾部会被砍掉。
- **渲染物落 `.tool_results/<run_id>/figures/<doc-sha>/page-NN.jpg`。** 不许新开目录、不许新开 TTL。
- **不动 `parse_image_ref`。** 它是明标的系统边界;新增兄弟解析器,复用 `artifact._validate_path` 的规则。
- **`_MAX_DOC_BYTES` 不动**(25 MB)。
- **xlsx 不做任何渲染。**
- 所有新注释与文档用简体中文,代码标识符英文 —— 与仓库现状一致。

---

## 文件结构

| 文件 | 职责 | 动作 |
|---|---|---|
| `packages/expert-work-runtime/src/expert_work/runtime/tokens.py` | token 估算 | 改:加每图代价 |
| `services/orchestrator/src/orchestrator/middleware_assembly.py` | 中间件装配 | 改:两个估算闭包换函数 |
| `services/orchestrator/src/orchestrator/tools/document_figures.py` | **新** 图清单:沙箱内 OOXML/PDF 探测片段 + 清单文本渲染 | 建 |
| `services/orchestrator/src/orchestrator/tools/read_document.py` | 文档转文字 | 改:接清单、三态、前置 |
| `services/orchestrator/src/orchestrator/tools/read_page.py` | **新** 按页渲染工具 | 建 |
| `packages/expert-work-protocol/src/expert_work/protocol/multimodal.py` | ref 解析(系统边界) | 改:**加兄弟**解析器 |
| `services/orchestrator/src/orchestrator/multimodal.py` | ImageResolver 实现 | 改:加 NAS 实现 |
| `services/orchestrator/src/orchestrator/tools/vision.py` | `ask_image` | 改:按 scheme 分派 |
| `packages/expert-work-common/src/expert_work/common/conversation_channel.py` | 段落标记 | 改:加 `FIGURE_BLOCK_MARK` |
| `services/orchestrator/src/orchestrator/state.py` | AgentState | 改:加 `viewed_figures` 通道 |
| `services/orchestrator/src/orchestrator/tools/registry.py` | 工具契约 | 改:允许新 state key |
| `services/orchestrator/src/orchestrator/graph_builder/builder.py` | 图节点 | 改:加图片段尾部注入 |

**为什么 `document_figures.py` 单开一个文件**:`read_document.py` 今天 260 行、职责单一(文档转文字)。图清单是另一件事(探测 + 决策数据),而且 `read_page.py` 也要用它的沙箱片段。两边都改 `read_document.py` 会让它变成两个职责的容器。

---

## Task 1: 估算器认图

**为什么先做**:Path A(Task 7)把图挂进提示词,而今天裁剪 / 压缩 / working window 三个闸门对图片是**瞎的** —— `image_ref` 块只按它的字符串表示计入,约 20 token,真实 1000–1300,低估约 65 倍。不先修,Task 7 一上线三个闸门就会一起失灵。

这条缺陷**今天已经存在**(用户自己传图就在低估),所以本任务独立可测、独立可合。

**Files:**
- Modify: `packages/expert-work-runtime/src/expert_work/runtime/tokens.py`
- Modify: `services/orchestrator/src/orchestrator/middleware_assembly.py:196-199` 与 `:228-231`
- Test: `packages/expert-work-runtime/tests/test_tokens.py`

**Interfaces:**
- Produces: `IMAGE_BLOCK_TOKEN_COST: int`、`count_image_blocks(msg: BaseMessage) -> int`、`estimate_message(msg: BaseMessage, estimator: TokenEstimator) -> int`
- Consumes: 已有的 `flatten_message` / `TokenEstimator.count` / `estimate_messages`

**⚠️ 关键约束:不许改 `flatten_message`。**

它有**两类**调用方,只有一类要图的代价:

| 调用方 | 用途 | 加了填充会怎样 |
|---|---|---|
| `middleware_assembly.py:198` / `:230` | 估算 | ✅ 正是要的 |
| `compressor.py:268` `estimate_messages` | 估算 | ✅ |
| `compressor.py:278` `_message_to_text` | **给摘要器造文本** | ❌ 喂进去一堆填充垃圾 |
| `llm/coalesce.py:91` | **合并 system 消息成真提示词** | ❌ 填充进真实提示词 |

所以代价加在**估算函数**上,不加在扁平化上。

- [ ] **Step 1: 写失败的测试**

追加到 `packages/expert-work-runtime/tests/test_tokens.py`:

```python
from langchain_core.messages import HumanMessage

from expert_work.runtime.tokens import (
    IMAGE_BLOCK_TOKEN_COST,
    count_image_blocks,
    default_estimator,
    estimate_message,
    flatten_message,
)


def _image_msg(n: int) -> HumanMessage:
    blocks: list[dict[str, str]] = [{"type": "text", "text": "看这几页"}]
    blocks += [{"type": "image_ref", "ref": f"expert_work://image/t/th/{i}.png"} for i in range(n)]
    return HumanMessage(content=blocks)


def test_image_blocks_are_counted() -> None:
    assert count_image_blocks(_image_msg(3)) == 3
    assert count_image_blocks(HumanMessage(content="纯文本")) == 0


def test_estimate_message_charges_for_images() -> None:
    est = default_estimator()
    text_only = est.count(flatten_message(_image_msg(0)))
    with_images = estimate_message(_image_msg(3), est)
    # 三张图必须至少多算三倍的每图代价,而不是多算一点点字符串表示。
    assert with_images >= text_only + 3 * IMAGE_BLOCK_TOKEN_COST


def test_image_cost_dwarfs_its_string_repr() -> None:
    """低估 65 倍就是这条测出来的:代价不能由 repr 的长度决定。"""
    est = default_estimator()
    one = _image_msg(1)
    repr_tokens = est.count(flatten_message(one))
    assert estimate_message(one, est) > 10 * repr_tokens


def test_flatten_message_is_not_padded() -> None:
    """``flatten_message`` 还要给 coalesce / 摘要器造**真文本**,不许掺填充。"""
    flat = flatten_message(_image_msg(2))
    assert "看这几页" in flat
    assert len(flat) < 500  # 两个 ref 的 repr 而已,没有 8000 字填充
```

- [ ] **Step 2: 跑,确认红**

```bash
uv run --no-sync pytest packages/expert-work-runtime/tests/test_tokens.py -k image -v
```

预期:`ImportError: cannot import name 'IMAGE_BLOCK_TOKEN_COST'`

- [ ] **Step 3: 实现**

在 `tokens.py` 里 `flatten_message` **之后**加:

```python
#: 一个图片内容块的 token 代价。
#:
#: 为什么需要这个常量:``flatten_message`` 把图片块折成它的字符串表示
#: (``{"type":"image_ref","ref":"expert_work://…"}`` 约 80 字符 ≈ 20 token),
#: 而一页 100 dpi 的渲染图按 ``宽×高/750`` 约 1000 token —— 低估约 65 倍。
#: 裁剪 / 压缩 / working window 三个闸门都靠估算值决定何时动手,低估的直接
#: 后果是它们对图片**完全看不见**:塞三张图进上下文,它们以为只加了 60 token。
#:
#: 取值:1568×1568(Anthropic 建议的长边上限)/750 ≈ 3277 是最坏情况;本设计
#: 渲染在 100 dpi、约 1.0 MP,实际约 1333。取 1300 作为常规档,宁可略低估
#: 最坏情况也不让常规档虚高 —— 虚高会让压缩过早触发,那是另一种失效。
#:
#: 两份参考实现都有同一个常量(hermes ``agent/image_token_cost.py``、
#: openclaw ``IMAGE_CHAR_ESTIMATE = 8_000``),它们同样把它喂给压缩触发器。
IMAGE_BLOCK_TOKEN_COST = 1_300

#: 计入 :data:`IMAGE_BLOCK_TOKEN_COST` 的块类型。``image_ref`` 是平台内部的
#: 引用块(J.6 Path A,字节由适配器在调用时解析);另外两个是厂商线格式,
#: 适配器翻译后的形状,历史重放时可能出现。
_IMAGE_BLOCK_TYPES = frozenset({"image_ref", "image", "image_url"})


def count_image_blocks(msg: BaseMessage) -> int:
    """消息内容里的图片块个数(``str`` 内容恒为 0)。"""
    content: Any = msg.content
    if isinstance(content, str):
        return 0
    return sum(
        1
        for block in content
        if isinstance(block, dict) and block.get("type") in _IMAGE_BLOCK_TYPES
    )


def estimate_message(msg: BaseMessage, estimator: TokenEstimator) -> int:
    """单条消息的 token 估算 —— 文本走 ``estimator``,图片走固定档。

    **不要把这个代价加进** :func:`flatten_message`:那个函数还要给
    ``llm.coalesce``(把多条 system 合成真提示词)和压缩器的摘要格式化造
    **真文本**,掺进填充会直接污染发给模型的内容。代价只加在估算这一侧。
    """
    return estimator.count(flatten_message(msg)) + count_image_blocks(msg) * IMAGE_BLOCK_TOKEN_COST
```

把 `estimate_messages` 改成走它:

```python
def estimate_messages(messages: Sequence[BaseMessage], estimator: TokenEstimator) -> int:
    """Per-message estimate sum over ``messages`` via ``estimator``(含图片代价)。"""
    return sum(estimate_message(msg, estimator) for msg in messages)
```

`middleware_assembly.py` 两处闭包(`:196-199` 与 `:228-231`)改成:

```python
        def _per_message(msg: BaseMessage) -> int:
            return estimate_message(msg, shared)
```

并把顶部 import 改为 `from expert_work.runtime.tokens import TokenEstimator, estimate_message`(`flatten_message` 若无其他用处则移除)。

- [ ] **Step 4: 跑,确认绿**

```bash
uv run --no-sync pytest packages/expert-work-runtime/tests/test_tokens.py -v
uv run --no-sync pytest services/orchestrator/tests/ -k "middleware or compress or context" -q
```

- [ ] **Step 5: 变异自证**

把 `IMAGE_BLOCK_TOKEN_COST` 临时改成 `0`,跑 `-k image`,确认 `test_estimate_message_charges_for_images` 与 `test_image_cost_dwarfs_its_string_repr` **变红**;改回去确认变绿。

⚠️ 变异前先 `git status --porcelain`,非空先 commit。还原后 `git diff` 确认为空。

- [ ] **Step 6: 提交**

```bash
git add packages/expert-work-runtime/src/expert_work/runtime/tokens.py \
        packages/expert-work-runtime/tests/test_tokens.py \
        services/orchestrator/src/orchestrator/middleware_assembly.py
git commit -m "fix(tokens): 估算器按固定档计图片块代价,不再按字符串表示

image_ref 块此前只按 repr 计入(约 20 token),而一页渲染图约 1300 ——
低估约 65 倍。裁剪/压缩/working window 三个闸门都靠估算值决定何时动手,
低估让它们对图片完全看不见。

代价加在新的 estimate_message 上而不是 flatten_message 里:后者还要给
llm.coalesce(合成真 system 提示词)和压缩器的摘要格式化造真文本,掺填充
会污染发给模型的内容。"
```

---

## Task 2: OOXML 图清单(来源侧判定)

**Files:**
- Create: `services/orchestrator/src/orchestrator/tools/document_figures.py`
- Test: `services/orchestrator/tests/test_document_figures.py`

**Interfaces:**
- Produces:
  - `build_figure_inventory_wrapper(rel: str, *, ws: str, max_bytes: int) -> str` —— 沙箱片段,打印 JSON 信封
  - `render_figure_map(env: Mapping[str, Any]) -> str` —— 信封转成给模型看的文本块(空字符串 = 不加块)
  - `MIN_FIGURE_EDGE_PT = 40`、`MIN_FIGURE_AREA_RATIO = 0.01`、`MAX_MAP_ENTRIES = 20`、`MAX_NOTES_CHARS = 500`
- Consumes: `orchestrator.tools.file_ops._snippet`、`_PRELUDE`(已有)

**信封形状**(后续任务按这个读,不许改字段名):

```python
{
  "ok": True,
  "state": "figures" | "none" | "undetermined",
  "reason": "soffice_missing" | "parse_failed" | ...,   # 仅 state=="undetermined"
  "format": "pptx",
  "figures": [
     {"unit": 3,                      # pptx=slide 号;docx=段落序号;pdf=页号
      "kind": "picture" | "chart" | "diagram" | "scanned_page",
      "count": 2,
      "w_pt": 288.0, "h_pt": 192.0,   # 显示尺寸(装饰图已滤掉)
      "anchor": "二、近三个月体重与血糖趋势",   # 紧邻的前文
      "alt": "血糖趋势图",              # OOXML alt 文字,无则 None
      "title": "趋势",                 # pptx slide 标题,无则 None
      "chart_data": {"cats": [...], "series": {...}},   # kind=="chart" 时
      "notes": "…"},                  # pptx 演讲者备注,已截断到 500 字
  ],
  "skipped_decorative": 1,
}
```

- [ ] **Step 1: 写失败的测试**

新建 `services/orchestrator/tests/test_document_figures.py`:

```python
"""``document_figures`` —— 来源侧图清单。

两层,与 test_read_document 同构:
  1. 沙箱内探测片段,拿本地 temp 工作区直接跑;
  2. 信封 → 清单文本的渲染。
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from orchestrator.tools.document_figures import (
    MIN_FIGURE_EDGE_PT,
    build_figure_inventory_wrapper,
    render_figure_map,
)


def _run(tmp_path: Path, rel: str) -> dict:
    """本地执行沙箱片段(与 test_read_document 的做法一致)。"""
    code = build_figure_inventory_wrapper(rel, ws=str(tmp_path), max_bytes=25 * 1024 * 1024)
    ns: dict = {}
    import contextlib

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        exec(compile(code, "<snippet>", "exec"), ns)  # noqa: S102
    return json.loads(buf.getvalue())


def _png(w: int, h: int) -> io.BytesIO:
    Image = pytest.importorskip("PIL.Image")
    b = io.BytesIO()
    Image.new("RGB", (w, h), "#cde").save(b, "PNG")
    b.seek(0)
    return b


def test_pptx_picture_slide_is_listed(tmp_path: Path) -> None:
    pptx = pytest.importorskip("pptx")
    from pptx.util import Inches

    prs = pptx.Presentation()
    s1 = prs.slides.add_slide(prs.slide_layouts[5])
    s1.shapes.title.text = "封面"
    s1.shapes.add_picture(_png(800, 500), Inches(1), Inches(2), width=Inches(5))
    prs.save(tmp_path / "d.pptx")

    env = _run(tmp_path, "d.pptx")
    assert env["ok"] is True
    assert env["state"] == "figures"
    assert env["format"] == "pptx"
    assert [f["unit"] for f in env["figures"]] == [1]
    assert env["figures"][0]["kind"] == "picture"
    assert env["figures"][0]["title"] == "封面"


def test_pptx_native_chart_becomes_data_not_a_figure(tmp_path: Path) -> None:
    """图表是数据,不该逼模型去看图。"""
    pptx = pytest.importorskip("pptx")
    from pptx.chart.data import CategoryChartData
    from pptx.enum.chart import XL_CHART_TYPE
    from pptx.util import Inches

    prs = pptx.Presentation()
    s = prs.slides.add_slide(prs.slide_layouts[5])
    s.shapes.title.text = "趋势"
    cd = CategoryChartData()
    cd.categories = ["7月", "8月", "9月"]
    cd.add_series("血糖", (6.1, 5.8, 5.5))
    s.shapes.add_chart(XL_CHART_TYPE.LINE, Inches(1), Inches(1.5), Inches(6), Inches(4), cd)
    prs.save(tmp_path / "c.pptx")

    env = _run(tmp_path, "c.pptx")
    chart = next(f for f in env["figures"] if f["kind"] == "chart")
    assert chart["chart_data"]["cats"] == ["7月", "8月", "9月"]
    assert chart["chart_data"]["series"] == {"血糖": [6.1, 5.8, 5.5]}


def test_decorative_logo_is_skipped(tmp_path: Path) -> None:
    """页脚 logo 29x29pt 占页 0.17%,不进清单;同页的 288x192pt 图表进。"""
    docx = pytest.importorskip("docx")
    from docx.shared import Inches

    d = docx.Document()
    d.add_heading("一、体检概览", 1)
    d.add_paragraph("这是第一段正文，后面跟着一张血糖趋势图。")
    d.add_picture(_png(600, 400), width=Inches(4))
    d.add_paragraph("下面是一张小 logo。")
    d.add_picture(_png(40, 40), width=Inches(0.4))
    d.save(tmp_path / "d.docx")

    env = _run(tmp_path, "d.docx")
    assert len(env["figures"]) == 1
    assert env["skipped_decorative"] == 1
    assert env["figures"][0]["w_pt"] == pytest.approx(288.0, abs=1.0)
    assert min(env["figures"][0]["w_pt"], env["figures"][0]["h_pt"]) >= MIN_FIGURE_EDGE_PT


def test_docx_anchor_is_the_sentence_before_the_figure(tmp_path: Path) -> None:
    docx = pytest.importorskip("docx")
    from docx.shared import Inches

    d = docx.Document()
    d.add_heading("一、体检概览", 1)
    d.add_paragraph("这是第一段正文，后面跟着一张血糖趋势图。")
    d.add_picture(_png(600, 400), width=Inches(4))
    d.save(tmp_path / "a.docx")

    env = _run(tmp_path, "a.docx")
    assert "血糖趋势图" in env["figures"][0]["anchor"]


def test_plain_table_is_not_a_figure(tmp_path: Path) -> None:
    """纯表格零误报 —— 表格不是 drawing,没有 graphicData。"""
    docx = pytest.importorskip("docx")

    d = docx.Document()
    d.add_heading("表格页", 1)
    t = d.add_table(rows=6, cols=4)
    for r in range(6):
        for c in range(4):
            t.cell(r, c).text = f"{r}-{c}"
    d.save(tmp_path / "t.docx")

    env = _run(tmp_path, "t.docx")
    assert env["state"] == "none"
    assert env["figures"] == []


def test_xlsx_never_asks_for_pixels(tmp_path: Path) -> None:
    """xlsx 的图表是单元格引用;不渲染,只报引用。"""
    openpyxl = pytest.importorskip("openpyxl")
    from openpyxl.chart import LineChart, Reference

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "体检"
    for row in (["月", "血糖"], ["7月", 6.1], ["8月", 5.8], ["9月", 5.5]):
        ws.append(row)
    ch = LineChart()
    ch.add_data(Reference(ws, min_col=2, min_row=1, max_row=4), titles_from_data=True)
    ws.add_chart(ch, "D2")
    wb.save(tmp_path / "s.xlsx")

    env = _run(tmp_path, "s.xlsx")
    assert all(f["kind"] != "picture" for f in env["figures"])
    chart = next(f for f in env["figures"] if f["kind"] == "chart")
    assert "体检" in json.dumps(chart["chart_data"], ensure_ascii=False)


def test_corrupt_file_is_undetermined_not_none(tmp_path: Path) -> None:
    """「测不了」必须与「没有图」分开 —— 三态的核心。"""
    (tmp_path / "bad.pptx").write_bytes(b"not a zip at all")
    env = _run(tmp_path, "bad.pptx")
    assert env["state"] == "undetermined"
    assert env["state"] != "none"


# --- 清单文本渲染 -----------------------------------------------------------


def test_map_says_it_could_not_tell() -> None:
    text = render_figure_map({"ok": True, "state": "undetermined", "reason": "parse_failed"})
    assert "无法确定" in text


def test_map_is_empty_when_no_figures() -> None:
    assert render_figure_map({"ok": True, "state": "none", "figures": []}) == ""


def test_map_forbids_rendering_everything() -> None:
    env = {
        "ok": True,
        "state": "figures",
        "format": "pptx",
        "figures": [{"unit": 3, "kind": "picture", "count": 1, "w_pt": 288.0,
                     "h_pt": 192.0, "anchor": "趋势", "alt": None, "title": "趋势"}],
        "skipped_decorative": 0,
    }
    text = render_figure_map(env)
    assert "不要把所有" in text
    assert "read_page" in text
    assert "OCR" not in text.upper() or "技能" in text  # 不点名具体 skill


def test_map_caps_entries() -> None:
    env = {
        "ok": True, "state": "figures", "format": "pptx", "skipped_decorative": 0,
        "figures": [{"unit": i, "kind": "picture", "count": 1, "w_pt": 288.0,
                     "h_pt": 192.0, "anchor": "", "alt": None, "title": None}
                    for i in range(1, 31)],
    }
    text = render_figure_map(env)
    assert text.count("\n  ") <= 22          # 20 条 + 折叠行的余量
    assert "另有" in text
```

- [ ] **Step 2: 跑,确认红**

```bash
uv run --no-sync pytest services/orchestrator/tests/test_document_figures.py -v
```

预期:`ModuleNotFoundError: No module named 'orchestrator.tools.document_figures'`

- [ ] **Step 3: 实现**

新建 `services/orchestrator/src/orchestrator/tools/document_figures.py`。骨架如下 —— 沙箱片段用 `_snippet` 组装,与 `read_document` 同一口径:

```python
"""文档图清单 —— B-64。

**判定只看来源侧 OOXML。** 最初设计想用 ``pdfplumber.Page.images`` 做统一探测器,
实测证伪:LibreOffice 把 EMF/WMF 与原生图表渲成**原生 PDF 矢量算子**而不是位图
XObject,``Page.images`` 对它们一律为 0 —— 照那个设计发出去,一页全是矢量图表的
slide 会被报成「这里没有图」,正是本功能要消灭的静默失效换一层重现。

``<a:graphicData uri>`` 反过来分得干净,而且**格式无关**:PNG 图和 EMF 图的 uri
完全相同(``…/drawingml/2006/picture``),所以判「有没有图」那一步永远不去碰可能
栅格化不了的字节(Pillow 在沙箱里 ``hasattr(Image.core, "drawwmf")`` 为 False)。
纯表格没有 ``graphicData`` —— 零误报。

**装饰图按显示尺寸过滤,不按原始像素**:一张 2000 px 的 logo 缩到 0.4 英寸摆在
页脚,像素很大但人看不见。实测分离是两个数量级 —— 页脚 logo 29×29 pt(占页
0.17 %)vs 血糖图表 288×192 pt(11.4 %)。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from orchestrator.tools.file_ops import _snippet

#: 显示短边下限(pt)。低于此的判为装饰图。
MIN_FIGURE_EDGE_PT: Final[float] = 40.0
#: 显示面积占页比下限。低于此的判为装饰图。
MIN_FIGURE_AREA_RATIO: Final[float] = 0.01
#: 清单条目上限 —— 交替出现的图文页会把清单撑爆。
MAX_MAP_ENTRIES: Final[int] = 20
#: pptx 演讲者备注的每页字数上限(§14.3:今天全环境 0 条,这是保险不是收益)。
MAX_NOTES_CHARS: Final[int] = 500
#: EMU → pt。914400 EMU = 1 英寸 = 72 pt。
_EMU_PER_PT: Final[int] = 12700

_FIGURE_INVENTORY_MAIN = '''
_PICTURE_URI = "http://schemas.openxmlformats.org/drawingml/2006/picture"
_CHART_URI = "http://schemas.openxmlformats.org/drawingml/2006/chart"
_DIAGRAM_URI = "http://schemas.openxmlformats.org/drawingml/2006/diagram"
_EMU_PER_PT = 12700
_MIN_EDGE_PT = 40.0
_MIN_AREA_RATIO = 0.01
_MAX_NOTES = 500


def _pptx_inventory(full):
    import pptx
    from pptx.util import Emu

    prs = pptx.Presentation(full)
    page_area_pt = (Emu(prs.slide_width).pt) * (Emu(prs.slide_height).pt)
    figures, skipped = [], 0
    for n, slide in enumerate(prs.slides, 1):
        title = None
        if slide.shapes.title is not None and slide.shapes.title.text_frame.text.strip():
            title = slide.shapes.title.text_frame.text.strip()
        notes = ""
        if slide.has_notes_slide:
            notes = slide.notes_slide.notes_text_frame.text.strip()[:_MAX_NOTES]
        pics, charts = [], []
        for sh in slide.shapes:
            if getattr(sh, "has_chart", False):
                ch = sh.chart
                try:
                    cats = [str(c) for c in ch.plots[0].categories]
                    series = {s.name: [None if v is None else float(v) for v in s.values]
                              for s in ch.series}
                except Exception:
                    cats, series = [], {}
                charts.append({"cats": cats, "series": series})
                continue
            if str(sh.shape_type).startswith("PICTURE") or str(sh.shape_type).startswith("GROUP"):
                w_pt, h_pt = Emu(sh.width).pt, Emu(sh.height).pt
                if min(w_pt, h_pt) < _MIN_EDGE_PT or (w_pt * h_pt) / page_area_pt < _MIN_AREA_RATIO:
                    skipped += 1
                    continue
                pics.append((w_pt, h_pt))
        for w_pt, h_pt in pics:
            figures.append({"unit": n, "kind": "picture", "count": 1,
                            "w_pt": round(w_pt, 1), "h_pt": round(h_pt, 1),
                            "anchor": title or "", "alt": None, "title": title,
                            "notes": notes})
        for data in charts:
            figures.append({"unit": n, "kind": "chart", "count": 1,
                            "w_pt": 0.0, "h_pt": 0.0, "anchor": title or "",
                            "alt": None, "title": title, "chart_data": data,
                            "notes": notes})
    return figures, skipped
'''
# …docx / xlsx / pdf 分支同构;完整实现见下方「实现要点」。
```

**实现要点**(逐条落进片段,不许省):

1. **docx**:走 `zipfile` + `ElementTree` 解 `word/document.xml`。按段落迭代,段内找 `{…wordprocessingDrawing}docPr`(拿 `name` / `descr` = alt 文字)与同级 `extent`(拿 `cx`/`cy` EMU)。`anchor` = 往回找最近一个有文字的段落,取其尾部 60 字符。页面面积按 `sectPr` 的 `pgSz` 算;拿不到就用 Letter(612×792 pt)。
2. **xlsx**:`openpyxl.load_workbook`,读 `ws._charts`(每个 series 的 `val.numRef.f` / `cat.numRef.f` / `tx.strRef.f` 是单元格引用字符串,直接作为 `chart_data`)与 `ws._images`。**xlsx 的 `kind` 只允许 `chart`**,图片一律按装饰图跳过 —— spec §9.4 定了 xlsx 不渲染。
3. **pdf**:`pdfplumber` 逐页 `extract_text()`,`len(strip()) < 20` 判空页;三重阈值(空页 ≥ 2 **且** 占比 ≥ 20 %,**或**绝对 ≥ 10)才产出 `kind="scanned_page"` 条目,连续页压成区间(`itertools.groupby(enumerate(empty), lambda e: e[1] - e[0])`),`anchor` = 往回找最近一个有字的页取尾部 60 字符。总页数 < 2 直接 `state="none"`。
4. **三态**:`zipfile.BadZipFile` / `ImportError` / 任何解析异常 → `{"ok": True, "state": "undetermined", "reason": ...}`。**绝不许返回 `state="none"`**。
5. `os.path.getsize(full) > _P["max_bytes"]` → `state="undetermined"`, `reason="file_too_large"`。

`render_figure_map` 的文案照 spec §10.2 的三条纪律:

```python
def render_figure_map(env: Mapping[str, Any]) -> str:
    """信封 → 前置在 ``content`` 头部的清单块(空串 = 不加块)。

    **前置不是尾置**:``read_document`` 返回 ``text[:cap]`` 硬截断,尾部的告警
    会被砍掉。hermes 出于同构的理由(它那边是 ``read_file`` 分页,footer 可能
    永远取不到)也是 PREPEND。
    """
    state = env.get("state")
    if state == "undetermined":
        return (
            "[图片情况无法确定:本文档的图片清单没能生成"
            f"(原因:{env.get('reason', 'unknown')})。"
            "**这不等于文档里没有图** —— 如果内容读起来有缺口,先怀疑这里。]\n\n"
        )
    figures = list(env.get("figures") or ())
    if state != "figures" or not figures:
        return ""
    # …逐条渲染,超过 MAX_MAP_ENTRIES 折叠成「… 另有 N 处」
```

块尾必须含这三句(逐字):

```
  这些内容不在上面的文字里。挑你真正需要的那几处 —— 不要把所有页都取一遍。
  要看某一处:调用 read_page,传文档路径和上面的编号。
  如果缺口很大而且都要看,先确认有没有可用的 OCR 技能(skills_list)。
```

- [ ] **Step 4: 跑,确认绿**

```bash
uv run --no-sync pytest services/orchestrator/tests/test_document_figures.py -v
```

- [ ] **Step 5: 变异自证(两条,都是结构性不变式)**

- M1:把 `_MIN_EDGE_PT` 改成 `0.0` → `test_decorative_logo_is_skipped` 必须**红**
- M2:把 `state="undetermined"` 那条分支改成 `state="none"` → `test_corrupt_file_is_undetermined_not_none` 必须**红**

各自还原并确认 `git diff` 为空。

- [ ] **Step 6: 提交**

```bash
git add services/orchestrator/src/orchestrator/tools/document_figures.py \
        services/orchestrator/tests/test_document_figures.py
git commit -m "feat(tools): 来源侧 OOXML 图清单(pptx/docx/xlsx/pdf)

判定看 graphicData uri 不看 PDF 绘图算子:LibreOffice 把 EMF/WMF 与原生
图表渲成矢量算子而非位图 XObject,pdfplumber.Page.images 对它们一律为 0。
uri 反过来格式无关(PNG 与 EMF 同一个 uri),所以判有没有图那一步永远不碰
可能栅格化不了的字节。纯表格没有 graphicData,零误报。

装饰图按显示尺寸过滤(短边 40pt / 占页 1%),不按原始像素。
三态:测不了必须与没有图分开。"
```

---

## Task 3: `read_document` 接上清单

**Files:**
- Modify: `services/orchestrator/src/orchestrator/tools/read_document.py`
- Test: `services/orchestrator/tests/test_read_document.py`

**Interfaces:**
- Consumes: Task 2 的 `build_figure_inventory_wrapper` / `render_figure_map`
- Produces: `ReadDocumentTool.call` 返回的 `ToolResult.content` 前置清单块;`meta` 新增 `figures`(条目数)与 `figures_state`

**关键:不许跑 LibreOffice。** 本任务只做清单 —— 清单来自 zip 里的 XML,约 1 ms。转换推迟到 Task 5 的 `read_page`。

- [ ] **Step 1: 写失败的测试**

追加到 `services/orchestrator/tests/test_read_document.py`:

```python
@pytest.mark.anyio
async def test_content_is_prefixed_with_the_figure_map() -> None:
    """清单必须在头部 —— content 是 text[:cap] 硬截断,尾部会被砍掉。"""
    runtime = _SequenceRuntime(
        [
            SandboxOutcome(stdout=json.dumps({"ok": True, "content": "正文" * 100,
                                              "format": "pptx", "chars": 200,
                                              "truncated": False}), stderr="", exit_code=0),
            SandboxOutcome(stdout=json.dumps({"ok": True, "state": "figures",
                                              "format": "pptx", "skipped_decorative": 0,
                                              "figures": [{"unit": 3, "kind": "picture",
                                                           "count": 1, "w_pt": 288.0,
                                                           "h_pt": 192.0, "anchor": "趋势",
                                                           "alt": None, "title": "趋势"}]}),
                          stderr="", exit_code=0),
        ]
    )
    tool = ReadDocumentTool(client=runtime)
    result = await tool.call({"path": "d.pptx"}, ctx=_ctx())
    head = result.content[:300]
    assert "read_page" in head
    assert result.content.index("read_page") < result.content.index("正文")
    assert result.meta["figures"] == 1
    assert result.meta["figures_state"] == "figures"


@pytest.mark.anyio
async def test_no_figures_leaves_content_byte_identical() -> None:
    """零图路径不许被碰 —— 这是「不回归」的钉子。"""
    body = "纯文字文档"
    runtime = _SequenceRuntime(
        [
            SandboxOutcome(stdout=json.dumps({"ok": True, "content": body, "format": "docx",
                                              "chars": len(body), "truncated": False}),
                           stderr="", exit_code=0),
            SandboxOutcome(stdout=json.dumps({"ok": True, "state": "none", "figures": []}),
                           stderr="", exit_code=0),
        ]
    )
    result = await ReadDocumentTool(client=runtime).call({"path": "d.docx"}, ctx=_ctx())
    assert result.content == body
    assert result.meta["figures"] == 0


@pytest.mark.anyio
async def test_inventory_failure_says_undetermined_not_silent() -> None:
    """清单探测本身失败 → 正文照给,但必须显式说「测不了」。"""
    runtime = _SequenceRuntime(
        [
            SandboxOutcome(stdout=json.dumps({"ok": True, "content": "正文", "format": "pptx",
                                              "chars": 2, "truncated": False}),
                           stderr="", exit_code=0),
            SandboxOutcome(stdout="", stderr="boom", exit_code=1),
        ]
    )
    result = await ReadDocumentTool(client=runtime).call({"path": "d.pptx"}, ctx=_ctx())
    assert "无法确定" in result.content
    assert result.meta["figures_state"] == "undetermined"
    assert "正文" in result.content
```

(`_ctx()` 复用文件里已有的 `ToolContext` 构造;若尚无,照 `test_tool_parses_envelope_into_result` 里的写法提出一个模块级辅助。)

- [ ] **Step 2: 跑,确认红**

```bash
uv run --no-sync pytest services/orchestrator/tests/test_read_document.py -k "figure or undetermined or byte_identical" -v
```

- [ ] **Step 3: 实现**

`read_document.py` 的 `call` 改成两段(正文 + 清单),清单失败**不拖垮正文**:

```python
    async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
        raw = _require_path(args, tool="read_document", agent_key=ctx.agent_key)
        ws, rel = resolve_scope(raw, agent_key=ctx.agent_key, tool="read_document")
        env = await run_scoped_read(
            self.client,
            build=lambda w: build_read_document_wrapper(rel, cap=self.output_char_cap, ws=w),
            ws=ws,
            ctx=ctx,
            tool="read_document",
            seed_files=self.skill_seed_files,
        )
        _raise_for_error(env, tool="read_document")

        # B-64 —— 图清单。**只读 zip 里的 XML(~1 ms),不跑 LibreOffice**:转换
        # 推迟到模型真的调 ``read_page`` 那一刻(spec § 四)。清单这一段失败绝不
        # 能拖垮正文 —— 但也绝不能静默降级成「没有图」,所以异常走 undetermined。
        try:
            fig_env: Mapping[str, Any] = await run_scoped_read(
                self.client,
                build=lambda w: build_figure_inventory_wrapper(
                    rel, ws=w, max_bytes=_MAX_DOC_BYTES
                ),
                ws=ws,
                ctx=ctx,
                tool="read_document",
                seed_files=self.skill_seed_files,
            )
        except Exception:  # noqa: BLE001 — 任何失败都只降级到「测不了」
            fig_env = {"ok": True, "state": "undetermined", "reason": "probe_failed"}

        prefix = render_figure_map(fig_env)
        body = str(env.get("content", ""))
        return ToolResult(
            content=prefix + body,
            meta={
                "path": rel,
                "format": env.get("format"),
                "chars": env.get("chars"),
                "truncated": bool(env.get("truncated")),
                "figures": len(fig_env.get("figures") or ()),
                "figures_state": fig_env.get("state"),
            },
        )
```

顶部加 import:

```python
from orchestrator.tools.document_figures import (
    build_figure_inventory_wrapper,
    render_figure_map,
)
```

- [ ] **Step 4: 跑,确认绿**

```bash
uv run --no-sync pytest services/orchestrator/tests/test_read_document.py -v
```

- [ ] **Step 5: 提交**

```bash
git add services/orchestrator/src/orchestrator/tools/read_document.py \
        services/orchestrator/tests/test_read_document.py
git commit -m "feat(read_document): 正文前置图清单,零图路径字节不变

清单只读 zip 里的 XML(约 1ms),不跑 LibreOffice —— 转换推迟到 read_page。
清单探测失败降级为「测不了」而不是「没有图」,正文照给。"
```

---

## Task 4: `read_page` 渲染工具

**Files:**
- Create: `services/orchestrator/src/orchestrator/tools/read_page.py`
- Test: `services/orchestrator/tests/test_read_page.py`

**Interfaces:**
- Produces:
  - `ReadPageTool`(工具名 `read_page`,参数 `path: str`、`units: list[int]`)
  - `RENDER_DPI = 100`、`MAX_PAGES_PER_CALL = 3`、`MAX_RENDER_PIXELS = 12_000_000`
  - `figure_ref(tenant_id, user_id, run_id, doc_sha, page) -> str` —— 造 `expert_work://workspace/...` ref
  - `ToolResult.state_updates = {"viewed_figures": [ref, ...]}`(Task 7 消费)
- Consumes: Task 2 的常量

**沙箱片段做三件事**(顺序不许换):

1. `soffice --headless --norestore --convert-to pdf --outdir <本次专用目录> <doc>`
   ⚠️ **每次转换自己的 outdir** —— 输出名按 basename 派生,`x.docx` 与 `x.pptx` 同目录会静默覆盖。
2. `pdftoppm -jpeg -r 100 -f N -l N <pdf> <prefix>`
3. **glob** 找产物 —— 页号按总页数补零,22 页文档出 `prefix-01.jpg`。**不许拼文件名。**

落点:`.tool_results/<run_id>/figures/<doc-sha>/page-NN.jpg`

- [ ] **Step 1: 写失败的测试**

新建 `services/orchestrator/tests/test_read_page.py`:

```python
"""``read_page`` —— 按需渲染文档单页。"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest

from orchestrator.tools.read_page import (
    MAX_PAGES_PER_CALL,
    RENDER_DPI,
    ReadPageTool,
    build_render_wrapper,
)
from orchestrator.tools.sandbox import RecordingSandboxRuntime, SandboxOutcome


def test_render_dpi_keeps_every_page_under_the_vision_limit() -> None:
    """100 dpi 下四种页型都 ≤1.15 MP 且长边 ≤1568 —— 150 dpi 全超线。"""
    geometries = {
        "pptx 16:9": (13.333, 7.5),
        "pptx 4:3": (10.0, 7.5),
        "docx Letter": (8.5, 11.0),
        "docx A4": (8.27, 11.69),
    }
    for name, (w_in, h_in) in geometries.items():
        w, h = round(w_in * RENDER_DPI), round(h_in * RENDER_DPI)
        assert w * h <= 1_150_000, f"{name} 超过 1.15 MP"
        assert max(w, h) <= 1568, f"{name} 长边超过 1568"


def test_wrapper_uses_a_private_outdir_per_conversion() -> None:
    """soffice 输出名按 basename 派生 —— 共用 outdir 会静默覆盖。"""
    a = build_render_wrapper("uploads/report.docx", units=[1], ws="/workspace",
                             out_rel=".tool_results/r1/figures/aaa", dpi=RENDER_DPI)
    b = build_render_wrapper("uploads/report.pptx", units=[1], ws="/workspace",
                             out_rel=".tool_results/r1/figures/bbb", dpi=RENDER_DPI)
    assert "--outdir" in a and "--outdir" in b
    assert "aaa" in a and "bbb" in b


def test_wrapper_globs_instead_of_building_the_page_filename() -> None:
    """pdftoppm 按总页数补零 —— 22 页文档出 page-01.jpg。"""
    code = build_render_wrapper("d.pptx", units=[11], ws="/workspace",
                                out_rel=".tool_results/r1/figures/s", dpi=RENDER_DPI)
    assert "glob" in code
    assert "page-11.jpg" not in code


def test_wrapper_renders_jpeg_not_png() -> None:
    code = build_render_wrapper("d.pptx", units=[1], ws="/workspace",
                                out_rel=".tool_results/r1/figures/s", dpi=RENDER_DPI)
    assert "-jpeg" in code
    assert "-png" not in code


@pytest.mark.anyio
async def test_too_many_units_is_refused_with_a_reason() -> None:
    runtime = RecordingSandboxRuntime(SandboxOutcome(stdout="{}", stderr="", exit_code=0))
    tool = ReadPageTool(client=runtime)
    result = await tool.call(
        {"path": "d.pptx", "units": list(range(1, MAX_PAGES_PER_CALL + 5))}, ctx=_ctx()
    )
    assert "一次最多" in result.content
    assert result.state_updates.get("viewed_figures", []) == []


@pytest.mark.anyio
async def test_rendered_pages_become_refs_in_state() -> None:
    runtime = RecordingSandboxRuntime(
        SandboxOutcome(
            stdout=json.dumps({"ok": True, "rendered": [
                {"unit": 3, "rel": ".tool_results/r1/figures/abc/page-03.jpg", "bytes": 61000}]}),
            stderr="", exit_code=0)
    )
    result = await ReadPageTool(client=runtime).call(
        {"path": "d.pptx", "units": [3]}, ctx=_ctx()
    )
    refs = result.state_updates["viewed_figures"]
    assert len(refs) == 1
    assert refs[0].startswith("expert_work://workspace/")
    assert "page-03.jpg" in refs[0]


@pytest.mark.anyio
async def test_soffice_missing_is_undetermined_not_empty() -> None:
    runtime = RecordingSandboxRuntime(
        SandboxOutcome(stdout=json.dumps({"ok": False, "error": "soffice_missing"}),
                       stderr="", exit_code=0)
    )
    result = await ReadPageTool(client=runtime).call({"path": "d.pptx", "units": [1]}, ctx=_ctx())
    assert "无法" in result.content
    assert result.state_updates.get("viewed_figures", []) == []
```

- [ ] **Step 2: 跑,确认红**

```bash
uv run --no-sync pytest services/orchestrator/tests/test_read_page.py -v
```

- [ ] **Step 3: 实现**

`read_page.py` 的沙箱片段核心(用 `subprocess` + `glob`,`shutil.which` 先探):

```python
_RENDER_MAIN = '''
import glob as _glob
import shutil
import subprocess


def _main():
    full = _resolve(_P["rel"])
    if full is None:
        return {"ok": False, "error": "path_escapes_workspace"}
    if shutil.which("soffice") is None or shutil.which("pdftoppm") is None:
        return {"ok": False, "error": "soffice_missing"}
    out_dir = os.path.join(_P["ws"], _P["out_rel"])
    os.makedirs(out_dir, exist_ok=True)
    ext = os.path.splitext(full)[1].lower()
    if ext == ".pdf":
        pdf = full
    else:
        # 每次转换自己的 outdir —— soffice 的输出名按 basename 派生,
        # 同目录里 x.docx 与 x.pptx 会互相静默覆盖(实测撞到过)。
        conv = os.path.join(out_dir, "_pdf")
        os.makedirs(conv, exist_ok=True)
        try:
            subprocess.run(
                ["soffice", "--headless", "--norestore", "--convert-to", "pdf",
                 "--outdir", conv, full],
                capture_output=True, timeout=_P["convert_timeout_s"], check=False)
        except Exception as exc:
            return {"ok": False, "error": "convert_failed", "detail": type(exc).__name__}
        pdfs = _glob.glob(os.path.join(conv, "*.pdf"))
        if not pdfs:
            return {"ok": False, "error": "convert_failed"}
        pdf = pdfs[0]
    rendered = []
    for unit in _P["units"]:
        prefix = os.path.join(out_dir, "page")
        try:
            subprocess.run(
                ["pdftoppm", "-jpeg", "-r", str(_P["dpi"]),
                 "-f", str(unit), "-l", str(unit), pdf, prefix],
                capture_output=True, timeout=_P["render_timeout_s"], check=False)
        except Exception:
            continue
        # pdftoppm 把页号补零到总页数的宽度(22 页 → page-01.jpg),
        # 所以必须 glob,不许拼文件名。
        hits = sorted(_glob.glob(prefix + "-*" + str(unit) + ".jpg"))
        if not hits:
            hits = sorted(_glob.glob(prefix + "-*.jpg"))
        if not hits:
            continue
        got = hits[-1]
        rendered.append({"unit": unit,
                         "rel": os.path.relpath(got, _P["ws"]),
                         "bytes": os.path.getsize(got)})
    if not rendered:
        return {"ok": False, "error": "render_failed"}
    return {"ok": True, "rendered": rendered}


print(json.dumps(_main()))
'''
```

工具侧:

- `units` 超过 `MAX_PAGES_PER_CALL` → 直接返回说明文本,`state_updates` 空,**不进沙箱**
- `ok is False` → `content` 明说取不到及原因,`state_updates` 空
- 成功 → `content` 列出取到了哪几页 + 「已放进你的上下文 / 用 `ask_image` 问它」(按 Task 6/7 的能力分支给不同措辞),`state_updates={"viewed_figures": [...]}`
- `doc_sha` = `hashlib.sha256(rel.encode()).hexdigest()[:16]`
- 累计像素预算记在 `meta` 里由 Task 7 的块渲染时汇总;本任务先按每次调用的页数硬限

- [ ] **Step 4: 跑,确认绿 + 真沙箱冒烟**

```bash
uv run --no-sync pytest services/orchestrator/tests/test_read_page.py -v
```

真沙箱(测试集群)冒烟 —— 造一份 3 页 pptx,跑一次 `read_page`,确认拿到 jpg 且 ≤1.15 MP:

```bash
export KUBECONFIG=~/.kube/expert-work-test.yaml
# 在 default 命名空间的沙箱 pod 里跑,用完清理 /tmp
```

- [ ] **Step 5: 提交**

```bash
git add services/orchestrator/src/orchestrator/tools/read_page.py \
        services/orchestrator/tests/test_read_page.py
git commit -m "feat(tools): read_page —— 按需把文档单页渲成 jpeg

100 dpi 不是 150:四种页型在 150 下全部超过 Anthropic 的 1.15MP/1568px
线,服务端会缩回去,2.8 倍字节换零有效分辨率。

两个实测坑写进实现:soffice 输出名按 basename 派生(每次转换自己的
outdir),pdftoppm 页号按总页数补零(必须 glob 不许拼文件名)。"
```

---

## Task 5: Path B —— `ask_image` 收工作区 ref

**Files:**
- Modify: `packages/expert-work-protocol/src/expert_work/protocol/multimodal.py`
- Modify: `services/orchestrator/src/orchestrator/multimodal.py`
- Modify: `services/orchestrator/src/orchestrator/tools/vision.py`
- Test: `services/orchestrator/tests/test_multimodal.py`、`services/orchestrator/tests/test_vision_tool.py`

**Interfaces:**
- Produces:
  - `WORKSPACE_REF_PREFIX = "expert_work://workspace/"`
  - `parse_workspace_image_ref(uri: str) -> WorkspaceImageRef`(字段 `tenant_id: UUID`、`user_id: UUID`、`rel: str`)
  - `NasWorkspaceImageResolver`(实现已有的 `ImageResolver` Protocol)
- Consumes: Task 4 造出的 ref 形态

**⚠️ 不许动 `parse_image_ref`。** 它的 docstring 写着 "This is a system boundary";往里加分支等于把一个已加固的校验点重新打开。**加兄弟,不加分支。**

新解析器复用 `artifact._validate_path`(`tools/artifact.py:68`)的同一套规则:折叠绝对路径、拒 `..`、拒 `agents/` 与 `shared/` 首段。

- [ ] **Step 1: 写失败的测试**

```python
def test_workspace_ref_rejects_parent_traversal() -> None:
    with pytest.raises(ValueError):
        parse_workspace_image_ref(
            "expert_work://workspace/<t>/<u>/.tool_results/r1/../../etc/passwd"
        )


def test_workspace_ref_rejects_another_agents_subtree() -> None:
    with pytest.raises(ValueError):
        parse_workspace_image_ref("expert_work://workspace/<t>/<u>/agents/other/x.jpg")


def test_workspace_ref_roundtrips() -> None:
    t, u = uuid4(), uuid4()
    ref = f"expert_work://workspace/{t}/{u}/.tool_results/r1/figures/abc/page-03.jpg"
    parsed = parse_workspace_image_ref(ref)
    assert parsed.tenant_id == t
    assert parsed.rel.endswith("page-03.jpg")


def test_parse_image_ref_still_refuses_workspace_scheme() -> None:
    """老边界一个字没松 —— 这是「加兄弟不加分支」的钉子。"""
    with pytest.raises(ValueError):
        parse_image_ref("expert_work://workspace/t/u/x.jpg")


@pytest.mark.anyio
async def test_ask_image_accepts_a_workspace_ref(tmp_path: Path) -> None:
    """按 scheme 分派到 NAS resolver,VL 模型拿到的是真字节。"""
    tenant, user = uuid4(), uuid4()
    page = tmp_path / str(tenant) / str(user) / ".tool_results" / "r1" / "figures" / "abc"
    page.mkdir(parents=True)
    (page / "page-03.jpg").write_bytes(b"\xff\xd8\xff\xe0fake-jpeg")

    caller = _RecordingVLCaller(answer="曲线从 6.1 降到 5.5")
    tool = AskImageTool(
        vl_caller=caller,
        image_resolver=InMemoryImageResolver({}),
        workspace_image_resolver=NasWorkspaceImageResolver(root=tmp_path),
    )
    ref = f"expert_work://workspace/{tenant}/{user}/.tool_results/r1/figures/abc/page-03.jpg"
    result = await tool.call(
        {"image_ref": ref, "question": "走势如何"},
        ctx=_ctx(tenant_id=tenant, user_id=user),
    )
    assert "5.5" in result.content
    assert caller.seen_bytes == b"\xff\xd8\xff\xe0fake-jpeg"


@pytest.mark.anyio
async def test_ask_image_rejects_a_cross_tenant_workspace_ref(tmp_path: Path) -> None:
    """租户校验对两种 ref 都执行 —— 新 scheme 不是绕过它的后门。"""
    mine, theirs, user = uuid4(), uuid4(), uuid4()
    tool = AskImageTool(
        vl_caller=_RecordingVLCaller(answer="never"),
        image_resolver=InMemoryImageResolver({}),
        workspace_image_resolver=NasWorkspaceImageResolver(root=tmp_path),
    )
    ref = f"expert_work://workspace/{theirs}/{user}/.tool_results/r1/figures/a/page-01.jpg"
    with pytest.raises(ToolBlockedError):
        await tool.call({"image_ref": ref, "question": "?"}, ctx=_ctx(tenant_id=mine))
```

- [ ] **Step 2: 跑,确认红**

```bash
uv run --no-sync pytest services/orchestrator/tests/test_multimodal.py \
                       services/orchestrator/tests/test_vision_tool.py -k workspace -v
```

预期:`ImportError: cannot import name 'parse_workspace_image_ref'`

- [ ] **Step 3: 实现**

`packages/expert-work-protocol/src/expert_work/protocol/multimodal.py` —— **在 `parse_image_ref` 旁边加一个新函数,一个字都不改它**:

```python
WORKSPACE_REF_PREFIX: Final = "expert_work://workspace/"


@dataclass(frozen=True)
class WorkspaceImageRef:
    """工作区里一张图的引用 —— 平台自己渲出来的文档页(B-64)。"""

    tenant_id: UUID
    user_id: UUID
    rel: str
    ext: str


def parse_workspace_image_ref(uri: str) -> WorkspaceImageRef:
    """解析 ``expert_work://workspace/<tenant>/<user>/<rel>``;不合法抛 ``ValueError``。

    **这是 :func:`parse_image_ref` 的兄弟,不是它的分支。** 那一个是明标的系统
    边界(``image_ref`` 参数从 LLM 工具调用直达),往里加一条 scheme 分支等于把
    一个已加固的校验点重新打开 —— 每加一个形态,它要同时为两种形态负责,而两种
    形态的合法性规则并不相同。这里单独校验,规则与
    ``orchestrator.tools.artifact._validate_path`` 同款:拒绝绝对路径、拒绝
    ``..`` 段、拒绝 ``agents/`` 与 ``shared/`` 首段(别人的子树 / 只读共享区)。
    """
    if not uri.startswith(WORKSPACE_REF_PREFIX):
        msg = f"workspace image ref must start with {WORKSPACE_REF_PREFIX!r}: {uri!r}"
        raise ValueError(msg)
    parts = uri[len(WORKSPACE_REF_PREFIX) :].split("/")
    if len(parts) < 3:
        msg = f"workspace image ref must carry tenant/user/path: {uri!r}"
        raise ValueError(msg)
    try:
        tenant_id, user_id = UUID(parts[0]), UUID(parts[1])
    except ValueError as exc:
        msg = f"workspace image ref has a malformed tenant/user id: {uri!r}"
        raise ValueError(msg) from exc
    rel = "/".join(parts[2:])
    segments = PurePosixPath(rel).parts
    if not rel or rel.startswith("/") or ".." in segments:
        msg = f"workspace image ref path must be relative and free of '..': {uri!r}"
        raise ValueError(msg)
    if segments and segments[0] in ("agents", "shared"):
        msg = f"workspace image ref must not address the reserved {segments[0]}/ tree: {uri!r}"
        raise ValueError(msg)
    ext = PurePosixPath(rel).suffix.lower()
    if ext not in _MEDIA_TYPE_BY_EXT:
        msg = f"unsupported image extension {ext!r} in workspace ref {uri!r}"
        raise ValueError(msg)
    return WorkspaceImageRef(tenant_id=tenant_id, user_id=user_id, rel=rel, ext=ext)
```

`services/orchestrator/src/orchestrator/multimodal.py` 加一个 resolver(与
`ObjectStoreImageResolver` 并列):

```python
@dataclass(frozen=True)
class NasWorkspaceImageResolver:
    """:class:`ImageResolver` over the NAS-mounted workspace volume —— B-64。

    control-plane 已经挂着 ``/mnt/workspaces``,所以渲染页的字节直接从盘上读,
    不用走沙箱 ``exec`` 的 stdout(一页 base64 约 83 KB,没必要塞进管道)。
    """

    root: Path

    async def resolve(self, ref: str) -> ResolvedImage:
        parsed = parse_workspace_image_ref(ref)
        path = self.root / str(parsed.tenant_id) / str(parsed.user_id) / parsed.rel
        media_type = _MEDIA_TYPE_BY_EXT[parsed.ext]
        return ResolvedImage(data=path.read_bytes(), media_type=media_type)
```

`services/orchestrator/src/orchestrator/tools/vision.py` —— `AskImageTool` 多一个
可选字段,`call` 按前缀分派,**租户校验提到分派之前**:

```python
    vl_caller: LLMCaller
    image_resolver: ImageResolver
    #: B-64 —— 平台渲出来的文档页走工作区 ref;``None`` = 没装配这条路。
    workspace_image_resolver: ImageResolver | None = None
```

```python
        ref_str = _require_string(args, "image_ref")
        if ref_str.startswith(WORKSPACE_REF_PREFIX):
            if self.workspace_image_resolver is None:
                msg = "workspace image refs are not available for this agent"
                raise ToolBlockedError(msg)
            tenant_of_ref = parse_workspace_image_ref(ref_str).tenant_id
            resolver = self.workspace_image_resolver
        else:
            tenant_of_ref = parse_image_ref(ref_str).tenant_id
            resolver = self.image_resolver
        if tenant_of_ref != ctx.tenant_id:
            msg = "ask_image image_ref tenant does not match the run tenant"
            raise ToolBlockedError(msg)
```

`agent_factory.py` 组装 `AskImageTool` 时(`:835` 那一段)把
`workspace_image_resolver=NasWorkspaceImageResolver(root=...)` 一并注入;
root 从 settings 的工作区挂载点取,与 control-plane 的 `/mnt/workspaces` 同源。

- [ ] **Step 4: 跑,确认绿**

```bash
uv run --no-sync pytest services/orchestrator/tests/test_multimodal.py \
                       services/orchestrator/tests/test_vision_tool.py -v
```

- [ ] **Step 5: 变异自证**

把 `_validate_path` 的 `".." in parts` 判断临时去掉 → `test_workspace_ref_rejects_parent_traversal` 必须**红**。

- [ ] **Step 6: 提交**

```bash
git commit -m "feat(vision): ask_image 收工作区 ref,老边界一字未动

parse_image_ref 的 docstring 写着 This is a system boundary,往里加分支
等于把一个已加固的校验点重新打开。新增兄弟解析器,复用 artifact
._validate_path 的同一套规则,租户校验对两种 ref 都执行。"
```

---

## Task 6: Path A 的状态通道

**Files:**
- Modify: `packages/expert-work-common/src/expert_work/common/conversation_channel.py`
- Modify: `services/orchestrator/src/orchestrator/state.py`
- Modify: `services/orchestrator/src/orchestrator/tools/registry.py:372-374`
- Test: `services/orchestrator/tests/test_state_channels.py`(若无则新建)

**Interfaces:**
- Produces:
  - `FIGURE_BLOCK_MARK = "expert_work_figure_block"`
  - `AgentState["viewed_figures"]: NotRequired[Annotated[list[str], _merge_viewed_figures]]`
  - `TOOL_ALLOWED_STATE_KEYS` 增加 `"viewed_figures"`
- Consumes: Task 4 的 `state_updates`

**为什么要第三个标记 —— 写进 docstring,别只写在计划里:**

现有两个标记按**过期语义**分。`INPUTS_BLOCK_MARK` 过期 = 路径失效(压缩后把最新一段放回去);`WORKSPACE_BLOCK_MARK` 过期 = **内容本身在说谎**(去重,只留最新)。

**图是第三种:它不过期。** 一份上传文档的第 7 页渲出来是什么就永远是什么。用去重会在模型看第 7 页时丢掉还有用的第 3 页;用「放回最新」同理。所以它自己一条规矩:**累积 + 滑窗**。

`_merge_viewed_figures` 照 `promoted_tools` 的 `_merge_promoted` 写 —— 跨轮 union-dedupe 且**保序**(滑窗要按加入顺序取最新 N)。

- [ ] **Step 1: 写失败的测试**

```python
def test_viewed_figures_merges_across_turns_in_order() -> None:
    merged = _merge_viewed_figures(["a", "b"], ["c"])
    assert merged == ["a", "b", "c"]


def test_viewed_figures_dedupes_without_reordering() -> None:
    """重看同一页不该把它挪到队尾 —— 滑窗按「首次看到」排。"""
    assert _merge_viewed_figures(["a", "b"], ["a", "c"]) == ["a", "b", "c"]


def test_figure_mark_is_distinct_from_the_other_two() -> None:
    assert FIGURE_BLOCK_MARK not in {INPUTS_BLOCK_MARK, WORKSPACE_BLOCK_MARK}


def test_tools_may_write_viewed_figures() -> None:
    assert "viewed_figures" in TOOL_ALLOWED_STATE_KEYS
```

- [ ] **Step 2: 跑,确认红**

```bash
uv run --no-sync pytest services/orchestrator/tests/test_state_channels.py -v
```

- [ ] **Step 3: 实现**

`conversation_channel.py` —— 加在 `WORKSPACE_BLOCK_MARK` 之后:

```python
#: B-64 —— 平台按需渲出来的文档页(``graph_builder`` 的尾部隐藏 HumanMessage)。
#: 它同时带 :data:`HIDE_FROM_UI`;这个标记是**更窄**的一层。
#:
#: 与前两个的消费方式都**不是**一件事,别照着改。三者的区别在「过期」的含义:
#:
#: * :data:`INPUTS_BLOCK_MARK` 过期 = 里面的路径失效 → 压缩后把原件放回去;
#: * :data:`WORKSPACE_BLOCK_MARK` 过期 = **内容本身在说谎**(它逐字声称
#:   「/workspace 现在有什么」)→ dedup,只留最新;
#: * 本标记 —— **不过期**。一份上传文档的第 7 页,渲出来是什么就永远是什么。
#:
#: 所以前两条规矩套上来都是错的:dedup 会在模型看第 7 页时丢掉还有用的第 3 页,
#: 「放回最新」同理。它自己一条规矩 —— **累积 + 滑窗**(见
#: ``graph_builder._figure_block_tail``)。
FIGURE_BLOCK_MARK = "expert_work_figure_block"
```

并加进模块 `__all__`。

`state.py` —— 合并函数与通道:

```python
def _merge_viewed_figures(left: list[str], right: list[str]) -> list[str]:
    """跨轮累积已看过的页 ref —— union 去重且**保序**。

    保序是滑窗的前提:``_figure_block_tail`` 取「最新 N 条」靠的是列表次序。
    重看同一页不该把它挪到队尾 —— 那会把一张更早看过、模型还在用的图挤出窗口。
    照 ``_merge_promoted``(TE-6 ``promoted_tools``)的口径写。
    """
    seen = set(left)
    return [*left, *(ref for ref in right if not (ref in seen or seen.add(ref)))]
```

```python
    #: B-64 —— 本 run 里 ``read_page`` 渲出来、已经给过模型的页 ref,按首次
    #: 看到的次序。检查点里只有这些字符串(几十字节一条),**图片字节从不落库**:
    #: 块每轮由 ``_figure_block_tail`` 重建,与工作区快照同一口径(CM-C4)。
    viewed_figures: NotRequired[Annotated[list[str], _merge_viewed_figures]]
```

`registry.py:372-374` —— 允许工具写这个通道,并在上方的 Channels 注释里加一行:

```python
#: - ``viewed_figures`` —— B-64 ``read_page`` 追加它刚渲出来的页 ref
#:   (state.py 的通道用 ``_merge_viewed_figures`` 跨轮保序 union)。
TOOL_ALLOWED_STATE_KEYS: frozenset[str] = frozenset(
    {"plan", "subagent_invocations", "promoted_tools", "viewed_figures"}
)
```

- [ ] **Step 4: 跑,确认绿**

```bash
uv run --no-sync pytest services/orchestrator/tests/test_state_channels.py -v
uv run --no-sync pytest services/orchestrator/tests/ -k "state or registry" -q
```

- [ ] **Step 5: 提交**

```bash
git add packages/expert-work-common/src/expert_work/common/conversation_channel.py \
        services/orchestrator/src/orchestrator/state.py \
        services/orchestrator/src/orchestrator/tools/registry.py \
        services/orchestrator/tests/test_state_channels.py
git commit -m "feat(state): viewed_figures 通道 + FIGURE_BLOCK_MARK

第三个段落标记,因为过期语义与现有两个都不同:INPUTS 是路径失效、
WORKSPACE 是内容在说谎,而渲染页不过期。前两条规矩套上来都会丢掉还有用
的旧图,所以它自己一条:累积 + 滑窗。

合并函数保序,因为滑窗取「最新 N 条」靠列表次序 —— 重看同一页不该把它挪到
队尾,那会把一张更早看过、模型还在用的图挤出窗口。"
```

---

## Task 7: Path A 的块注入(滑窗 + 可见占位符)

**Files:**
- Modify: `services/orchestrator/src/orchestrator/graph_builder/builder.py`(挨着 `_workspace_block_tail`,约 `:2003`)
- Test: `services/orchestrator/tests/test_figure_block_injection.py`(照 `test_workspace_block_injection.py` 的写法)

**Interfaces:**
- Produces:`_figure_block_tail(messages, *, viewed, supports_vision, tenant_id, user_id) -> list[BaseMessage]`、`FIGURE_KEEP_RECENT = 3`
- Consumes: Task 6 的 `viewed_figures` / `FIGURE_BLOCK_MARK`

**架构要点 —— 我们比两份参考实现省掉一整层:**

hermes / openclaw 把 base64 塞进消息,所以「退役一张图」要重写多 MB 的消息,还得管检查点里的大块(`drop_stale_api_content` / `_strip_images_from_tool_msg`)。

我们存的是 **ref 字符串**(`image_ref_block(uri)` 返回 `{"type": "image_ref", "ref": uri}`),字节由适配器在**调用时**解析。所以:

- 检查点里只有 `viewed_figures` 的 ref 列表(几十字节一条)
- 块本身**每轮重建、从不落检查点** —— 与 `_workspace_block_tail` 同一口径(CM-C4),`test_the_block_never_lands_in_the_checkpoint` 是现成的先例
- 「退役」= 重建时少挂一个 ref,不是重写历史

**滑窗**:`FIGURE_KEEP_RECENT = 3`(hermes `_MAX_KEEP_TOOL_IMAGES = 3`、openclaw `keepLastAssistants: 3`,两家独立撞上同一个数)。

**退役是替换不是删除**:超窗的换成可见文字 —— 删除是静默失效,替换不是。

- [ ] **Step 1: 写失败的测试**

```python
def test_only_the_newest_three_figures_carry_pixels() -> None:
    msgs = _figure_block_tail([], viewed=[f"r{i}" for i in range(5)],
                              supports_vision=True, **_ids())
    block = msgs[-1]
    refs = [b for b in block.content if isinstance(b, dict) and b.get("type") == "image_ref"]
    assert len(refs) == 3
    assert [r["ref"] for r in refs] == ["r2", "r3", "r4"]


def test_retired_figures_leave_a_visible_placeholder() -> None:
    """删除是静默失效 —— 模型必须看得见这里原来有张图,以及怎么拿回来。"""
    msgs = _figure_block_tail([], viewed=[f"r{i}" for i in range(5)],
                              supports_vision=True, **_ids())
    text = "".join(b.get("text", "") for b in msgs[-1].content if isinstance(b, dict))
    assert "已退出上下文" in text
    assert "read_page" in text


def test_no_pixels_when_the_model_cannot_see() -> None:
    """非视觉主模型不挂 image 块 —— 那条路由走 ask_image(Path B)。"""
    msgs = _figure_block_tail([], viewed=["r0"], supports_vision=False, **_ids())
    assert all(
        b.get("type") != "image_ref"
        for m in msgs for b in (m.content if isinstance(m.content, list) else [])
        if isinstance(b, dict)
    )


def test_the_block_never_lands_in_the_checkpoint() -> None:
    """与工作区块同一口径 —— 块只进这一次的提示词视图,不进 state["messages"]。"""
    original: list[BaseMessage] = [HumanMessage(content="看看第三页")]
    prompt_view = _figure_block_tail(
        original, viewed=["r0"], supports_vision=True, **_ids()
    )
    assert len(prompt_view) == 2
    # 原列表一条没多 —— 块是新列表里的,不是塞回去的。
    assert len(original) == 1
    assert not any((m.additional_kwargs or {}).get(FIGURE_BLOCK_MARK) for m in original)


def test_previous_blocks_are_dropped_before_appending() -> None:
    prior = _figure_block_tail([], viewed=["r0"], supports_vision=True, **_ids())
    again = _figure_block_tail(prior, viewed=["r0", "r1"], supports_vision=True, **_ids())
    marked = [m for m in again if (m.additional_kwargs or {}).get(FIGURE_BLOCK_MARK)]
    assert len(marked) == 1
```

- [ ] **Step 2: 跑,确认红**

```bash
uv run --no-sync pytest services/orchestrator/tests/test_figure_block_injection.py -v
```

- [ ] **Step 3: 实现**

`builder.py` —— 挨着 `_workspace_block_tail`(约 `:2003`)加:

```python
#: 带像素进提示词的页数上限。超出的退役成可见占位符。
#:
#: 两份参考实现独立撞上同一个数 —— hermes ``_MAX_KEEP_TOOL_IMAGES = 3``、
#: openclaw ``keepLastAssistants: 3``。
FIGURE_KEEP_RECENT = 3


def _is_figure_block(msg: BaseMessage) -> bool:
    return isinstance(msg, HumanMessage) and bool(
        (msg.additional_kwargs or {}).get(FIGURE_BLOCK_MARK)
    )


def _figure_block_tail(
    messages: list[BaseMessage],
    *,
    viewed: list[str],
    supports_vision: bool,
    tenant_id: UUID,
    user_id: UUID,
) -> list[BaseMessage]:
    """B-64 —— 把已看过的文档页挂到提示词尾部;更早的那几段一并剔掉。

    **只进这一次的提示词视图,从不落检查点** —— 与 :func:`_workspace_block_tail`
    同一口径(CM-C4)。检查点里只有 ``state["viewed_figures"]`` 的 ref 字符串。

    这是我们比两份参考实现省掉的一整层:hermes / openclaw 把 base64 塞进消息,
    所以「退役一张图」要重写多 MB 的消息,还得管检查点里的大块
    (``drop_stale_api_content`` / ``_strip_images_from_tool_msg``)。我们存的是
    ref(``image_ref_block`` 返回 ``{"type": "image_ref", "ref": uri}``),字节由
    适配器在**调用时**解析 —— 退役只是重建时少挂一个 ref。

    **退役是替换不是删除。** 超窗的页换成可见文字,模型看得见这里原来有张图、
    也看得见怎么拿回来。删除是静默失效,而这个功能存在的全部意义就是消灭静默
    失效(spec 设计原则 1)。

    ``supports_vision`` 为假时不挂任何 image 块:那条路由走 ``ask_image``
    (Path B),字节从不进主上下文;在这里挂了它也看不见,只会白烧 token。
    """
    kept = [m for m in messages if not _is_figure_block(m)]
    if not viewed or not supports_vision:
        return kept
    live, retired = viewed[-FIGURE_KEEP_RECENT:], viewed[:-FIGURE_KEEP_RECENT]
    blocks: list[str | dict[Any, Any]] = [
        {
            "type": "text",
            "text": "# 你取来的文档页(这一轮可见)\n"
            + "".join(
                f"[图:{_ref_label(ref)} 已退出上下文。"
                "需要重看就再调一次 read_page。]\n"
                for ref in retired
            ),
        }
    ]
    blocks.extend(image_ref_block(ref) for ref in live)
    return [
        *kept,
        HumanMessage(
            content=blocks,
            additional_kwargs={HIDE_FROM_UI: True, FIGURE_BLOCK_MARK: True},
        ),
    ]
```

`_ref_label(ref)` 从 ref 末段取出 `page-03.jpg` 并渲成「第 3 页」。

挂在 `agent_node` 里 `_workspace_block_tail` 调用点(`builder.py:931`)之后,同一处:

```python
            messages = _figure_block_tail(
                messages,
                viewed=list(state.get("viewed_figures") or ()),
                supports_vision=built.supports_vision,
                tenant_id=tenant_id,
                user_id=user_id,
            )
```

- [ ] **Step 5: 变异自证(两条)**

- M1:`FIGURE_KEEP_RECENT` 改 `99` → `test_only_the_newest_three_figures_carry_pixels` **红**
- M2:把「替换成占位符」改成「直接丢掉」→ `test_retired_figures_leave_a_visible_placeholder` **红**

- [ ] **Step 6: 提交**

```bash
git commit -m "feat(graph): 渲染页按滑窗挂进提示词,退役留可见占位符

第三个段落标记,因为过期语义与现有两个都不同:INPUTS 是路径失效、
WORKSPACE 是内容在说谎,而渲染页不过期 —— 套任何一条现成规矩都会丢掉
还有用的旧图。所以它自己一条:累积 + 滑窗 3。

退役是替换不是删除:删除是静默失效,而这个功能存在的全部意义就是消灭
静默失效。

比 hermes/openclaw 省掉一层:我们历史里存 ref 不存 base64,字节由适配器
调用时解析,所以退役只是重建时少挂一个 ref,不用重写历史也不用管检查点
里的大块。"
```

---

## 收尾:验收

spec §13.1 的十条结构性不变式逐条跑,每条都要 break → red → restore → green 自证。其中第 7、9、10 条在本计划里分别由 Task 1、Task 3、Task 4 的测试覆盖;第 8 条由 Task 7 覆盖。

真栈(spec §13.2):用 `ai-health-plan` 的历史输入重放,对照组 = 当前 main。

- 带图 pptx 的 run:实验组**必须**出现 figure map
- 无图 docx 的 run:两组输出**必须字节一致**

---

## 自审

**1. spec 覆盖**

| spec 节 | 落在 |
|---|---|
| §5 来源侧判定 / 装饰图过滤 / PDF 例外 | Task 2 |
| §6 归一化 + 三个坑 | Task 4 |
| §7.1 100 dpi | Task 4(含四页型断言) |
| §7.2 两套预算 | Task 4(渲染档)/ Task 7(驻留档) |
| §8.1-8.2 三分投递 | Task 5(Path B)/ Task 7(Path A + 都不行) |
| §8.3 落 `.tool_results` | Task 4 |
| §8.4 ref 形态 | Task 5 |
| §8.5 新标记 + 滑窗 + 占位符 | Task 6 + Task 7 |
| §8.6 估算器缺陷 | Task 1 |
| §9 分格式行为 | Task 2(清单)/ Task 4(渲染) |
| §10 输出形状 | Task 2 的 `render_figure_map` |
| §11 三态 | Task 2 + Task 3 |
| §13 验收 | 收尾节 |

**2. 类型一致性**

- `build_figure_inventory_wrapper` / `render_figure_map`(Task 2)→ Task 3 消费,签名一致
- `viewed_figures: list[str]`(Task 4 写 / Task 6 定通道 / Task 7 读),三处同名同型
- `FIGURE_BLOCK_MARK`(Task 6 定 / Task 7 用)
- `RENDER_DPI = 100` 只在 Task 4 定义一处,Task 7 不复制

**3. 顺序依赖**

Task 1 独立(可先合)。2 → 3。2 → 4。4 → 5、4 → 6 → 7。
