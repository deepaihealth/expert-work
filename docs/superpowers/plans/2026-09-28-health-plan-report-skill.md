# 健康方案交付件平台技能（health-plan-report）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增平台技能 `health-plan-report`：把调用方 Agent 定稿的健康方案内容 JSON，按平台设计规范（方向 A「临床专业」）渲染成可编辑 PPTX 与字体嵌入的 A4 PDF，支持多层样式参数与自动质检；并把 ai-health-plan 迁移到该技能。

**Architecture:** 纯 Python 技能包（与 `platform-skills/docx|pptx|xlsx|pdf` 同构）。内容 JSON → 校验 → 积木映射为少量「版式原语」→ PPT：自研版式引擎（测量/分页/合并）+ python-pptx 原生对象；PDF：HTML/CSS + 内联 SVG 图表 → weasyprint 流式分页。样式由多层 JSON 叠加解析（大白话换算、品牌锁定、可读性纠正、参数落实清单）。质检（丢字/溢出/越界/对比度）是硬门槛。

**Tech Stack:** Python 3.12；python-pptx 1.0.2、weasyprint 70.0、Pillow 12.3、pypdf 6.18（沙箱镜像预装；本地仓库 venv 只有 python-pptx / Pillow / pdfplumber —— 本地测试不得依赖 weasyprint / pypdf / matplotlib）；pytest；ruff。

**Spec:** `docs/superpowers/specs/2026-09-28-health-plan-report-skill-design.md`

## Global Constraints

- **技能边界（首要原则）**：技能只呈现，不制定方案。渲染器不改、不补、不编、不删、不重排任何内容文字；章节与积木顺序按内容 JSON 原样；技能正文与脚本不出现健康红线、阈值、措辞规范等业务规则；不出现任何具体机构、客户、员工、Agent 名称（示例一律虚构，统一用「示例健康管理中心」「李先生」「王老师」）。
- 技能目录 `platform-skills/health-plan-report/`，技能名 `health-plan-report`；frontmatter `license: 深护智康自研，仅限本平台使用`（与现有平台技能逐字一致），`expert_work: {lazy: true, category: 健康}`；`description` ≤ 200 字；SKILL.md 正文（frontmatter 之后）≤ 6000 字符。
- 打包文件必须全是 UTF-8 文本（`test_threat_scans_pass_body_and_every_text_file` 会逐文件读）；不打包字体、图片、二进制。
- 脚本只用标准库 + python-pptx、Pillow、weasyprint、pypdf；**禁止** PyYAML、pydantic、matplotlib、Node/JS 路线。weasyprint 与 pypdf 只能在函数内懒导入（本地 venv 没有）。
- 平台 R14：看图核验只能 `read_page(path=成品, units=[…])` 后 `ask_image(path=成品, unit=…)`，不得把 PNG 路径交给 ask_image；技能不产出预览 PNG。
- PPT：16:9，`slide_width=12192000`、`slide_height=6858000` EMU（= 960×540 pt）；版式坐标一律以 pt 计算，写入时 `emu(pt)=round(pt*12700)`；中文字体 `a:ea=微软雅黑`、西文 `a:latin=Arial`；文字、表格、图表均为原生对象。
- PDF：A4 竖版，嵌入 Noto Sans CJK SC（沙箱 `/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc`，TTC 索引 SC=2）。
- 状态色固定：在参考范围内 `#2E7D5B`、高于/低于参考范围 `#B86A0C`、警示 `#B42318`；方向 A 默认主色 `#0B4F5C`、强调色 `#E8A33D`；正文墨色 `#1D2B30`。
- 可读性阈值：正文墨色/背景 ≥ 7:1；主色/背景 ≥ 4.5:1；背景只接受相对亮度 ≥ 0.80 的浅色。
- 版式：放不下就拆页（「（续）」）、表格拆页重复表头；**不缩字号、不压行距、不侵占页脚**；测量结果另留 15% 宽度余量（`SAFETY=1.15`），行高 `LINE=1.35×字号`。
- 输出文件已存在时拒绝覆盖；质检失败时成品改名为 `<basename>.qa-failed.<ext>` 并以退出码 1 结束。
- 测试命令：`uv run --no-sync pytest platform-skills/tests -q`；lint：先 `uv run ruff check --fix platform-skills && uv run ruff format platform-skills`（自动修导入顺序与格式），再 `uv run ruff check platform-skills && uv run ruff format --check platform-skills` 必须零报错；`sys.path.insert` 之后的导入若报 E402，照 `platform-skills/shared/preview.py` 的既有写法处理。每条新断言按 break → red → restore → green 自证后再提交。
- 提交信息结尾：
  ```
  Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01DNzoY8vc8jDMsib4wFGFpN
  ```

## Rulings made while planning（spec 未写细的地方，计划的裁定）

- R1 样式定位键：按积木类型全局设置用 `blocks.<kind>.variant`；按章节/积木精确定位用 `sections.<章节id>.variant`、`sections.<章节id>.<积木id或1起序号>.variant`（避免章节 id 与 kind 同名时歧义）。优先级：积木精确 > 章节 > 类型 > 默认。T2 同步改 spec §5.1 该行。
- R2 目录自动规则：PPT 章节数 ≥ 8、PDF 章节数 ≥ 6 时自动加目录（PDF 渲染前无法得知页数，改用章节数）。
- R3 页脚免责声明超过 2 行时，页脚区向上扩展（最多 4 行），正文区相应缩短；超过 4 行报错。
- R4 趋势图 `bar` 版式不画上下限系列，改为图下一行「目标区间 a–b 单位」说明文字。
- R5 PDF 页码格式固定为「第 N 页 / 共 M 页」，PDF 质检据此正则剔除页码后做丢字比对。
- R6 PPT 丢字比对语料：所有 `hpr:body` 形状按绘制顺序拼接 + 分隔符 + 所有 `hpr:chrome` 形状拼接；`hpr:deco`（生成的标记、刻度、序号）不计入。

## Review Focus

1. 一个没有空格的超长字符串（如 80 字符的编号或链接文字）放进卡片、表格单元格 → 应按字符折行，不溢出不报错（T4 测试 `test_unbreakable_token_wraps_in_card_and_table`）。
2. 内容极少（1 个章节 1 段文字）→ 只出封面 + 1 页内容，不出空白页（T4 `test_minimal_content_one_page`）。
3. 样式层文件是嵌套 JSON（`{"color": {"primary": "深蓝"}}`）或不是 JSON 对象 → 嵌套自动展平；非对象给出清晰报错并退出码 1（T2 `test_nested_layer_is_flattened`、`test_non_object_layer_rejected`）。
4. 健康画像某项没有参考范围但给了 `position` → 显示对照标签、不画范围条、不崩（T3 `test_profile_without_range_has_tag_no_bar`）。
5. 同一 basename 再渲染一次 → 拒绝覆盖旧成品、退出码 1、旧文件字节不变（T5 `test_existing_output_is_not_overwritten`）。

## 文件结构

```
platform-skills/health-plan-report/
  SKILL.md                      T1 占位 frontmatter + 最短正文；T9 写全
  skill.yaml                    shared: [_cli.py]
  reference/content-schema.md   T9
  reference/style-options.md    T9
  reference/migration.md        T9
  sample/sample-plan.json       T1 虚构完整样例（覆盖全部 24 种积木）
  scripts/validate.py           T1 CLI
  scripts/resolve_style.py      T2 CLI
  scripts/render.py             T5（pptx）/ T7（pdf）CLI
  scripts/hpr/__init__.py       T1
  scripts/hpr/catalog.py        T1 积木种类 + 可选版式 + 版式同义词
  scripts/hpr/content.py        T1 内容 schema、校验、文字清单
  scripts/hpr/style.py          T2 样式层解析
  scripts/hpr/theme.py          T3 主题 token
  scripts/hpr/measure.py        T3 文字测量与折行
  scripts/hpr/prims.py          T3 版式原语
  scripts/hpr/blocks.py         T3 积木 → 原语映射
  scripts/hpr/icons.py          T3 章节图标（折线数据）
  scripts/hpr/ppt_layout.py     T4 PPT 测高、拆分、分页
  scripts/hpr/ppt_draw.py       T5 PPT 绘制（页面框架、封面、目录、结束页、原语）
  scripts/hpr/ppt_charts.py     T5 PPT 原生图表
  scripts/hpr/qa.py             T5（pptx）/ T7（pdf）质检
  scripts/hpr/pdf_svg.py        T7 PDF 图表 SVG
  scripts/hpr/pdf_html.py       T7 PDF HTML/CSS
platform-skills/tests/health_plan_report/
  conftest.py                   T1 把 scripts/ 加入 sys.path + 夹具
  test_content.py               T1
  test_style.py                 T2
  test_theme_measure_blocks.py  T3
  test_ppt_layout.py            T4
  test_render_pptx.py           T5
  test_pdf.py                   T7
  test_matrix.py                T8
  test_skill_package.py         T9
platform-skills/tests/in_image/case_hpr_render.py   T7
platform-skills/tests/test_platform_checks.py       T1 改：office 四件套与新技能分开
```

## PR 切分

- PR1 = T1 + T2（模型、校验、样式解析）
- PR2 = T3 + T4 + T5 + T6（主题、测量、映射、版式引擎、PPT 输出、设计确认）
- PR3 = T7 + T8（PDF、质检、全组合矩阵、镜像内测试）
- PR4 = T9 + T10（技能正文与参考文档、ai-health-plan 迁移、真栈验收）

---
### Task 1: 技能骨架 + 内容 JSON 模型与校验

**Files:**
- Create: `platform-skills/health-plan-report/SKILL.md`（占位，T9 写全）
- Create: `platform-skills/health-plan-report/skill.yaml`
- Create: `platform-skills/health-plan-report/scripts/hpr/__init__.py`
- Create: `platform-skills/health-plan-report/scripts/hpr/catalog.py`
- Create: `platform-skills/health-plan-report/scripts/hpr/content.py`
- Create: `platform-skills/health-plan-report/scripts/validate.py`
- Create: `platform-skills/health-plan-report/sample/sample-plan.json`
- Create: `platform-skills/tests/health_plan_report/conftest.py`
- Create: `platform-skills/tests/health_plan_report/test_content.py`
- Modify: `platform-skills/tests/test_platform_checks.py`（`EXPECTED_SKILLS` → `OFFICE_SKILLS`，总数断言加上新技能）
- Modify: `platform-skills/tests/conftest.py:8`（同名常量改为 `ALL_SKILLS`）

**Interfaces:**
- Produces:
  - `hpr.catalog.KINDS: tuple[str, ...]`、`VARIANTS: dict[str, tuple[str, ...]]`（首项为默认）、`VARIANT_SYNONYMS: dict[str, str]`
  - `hpr.content.ContentError(path: str, message: str)`（frozen dataclass，`str()` = `"path: message"`）
  - `hpr.content.load_content(path: Path) -> dict`（非 JSON / 非对象 → `ValueError`，消息为中文）
  - `hpr.content.validate_content(content: dict) -> list[ContentError]`
  - `hpr.content.required_texts(content: dict) -> list[str]`
  - `hpr.content.iter_blocks(content) -> Iterator[tuple[int, str, int, dict]]`（章节序号, 章节 id, 积木 1 起序号, 积木）
  - CLI：`python validate.py CONTENT.json` → stdout `{"ok": true, "sections": N}` 退出 0；或 `{"ok": false, "errors": [...]}` 退出 1

- [ ] **Step 1: 建目录与占位文件**

`platform-skills/health-plan-report/skill.yaml`：

```yaml
shared:
  - _cli.py
```

`platform-skills/health-plan-report/SKILL.md`（T9 会整体重写；此处只为让 `build.py` 识别本技能目录）：

```markdown
---
name: health-plan-report
description: 把已定稿的健康管理方案内容（按本技能的内容 JSON 格式）渲染成专业的可编辑 PPT 与 A4 PDF。只负责呈现，不制定方案内容。
license: 深护智康自研，仅限本平台使用
expert_work:
  lazy: true
  category: 健康
---

（正文在后续任务补全。）

校验内容：`python $EXPERT_WORK_SKILLS_DIR/health-plan-report/scripts/validate.py 内容.json`
```

`platform-skills/health-plan-report/scripts/hpr/__init__.py`：

```python
"""health-plan-report renderer package (platform skill)."""
```

- [ ] **Step 2: 写 `hpr/catalog.py`**

```python
"""Block kinds and their selectable layout variants (first variant = default)."""

from __future__ import annotations

VARIANTS: dict[str, tuple[str, ...]] = {
    "summary": ("columns", "list"),
    "profile": ("cards", "table"),
    "issues": ("cards", "list"),
    "trend": ("line", "bar"),
    "goals": ("cards", "table"),
    "phases": ("timeline", "columns", "table"),
    "nutrition": ("donut", "table"),
    "meal_plan": ("timeline", "table", "cards"),
    "diet_rules": ("columns", "list"),
    "exercise": ("fitt", "cards"),
    "sleep": ("timebar", "list"),
    "stress": ("cards", "list"),
    "habits": ("cards", "table"),
    "material": ("card",),
    "monitoring": ("table", "cards"),
    "referral": ("box",),
    "shopping": ("columns", "list"),
    "follow_up": ("cards", "table"),
    "paragraph": ("plain", "boxed"),
    "bullets": ("dots", "numbers", "checks"),
    "table": ("table",),
    "kv": ("two-column", "one-column"),
    "image": ("fit",),
    "callout": ("box",),
}

KINDS: tuple[str, ...] = tuple(VARIANTS)

#: 用户 / Agent 常用说法 → 版式名。只收录不会跨积木歧义的词。
VARIANT_SYNONYMS: dict[str, str] = {
    "卡片": "cards",
    "按天卡片": "cards",
    "表格": "table",
    "列表": "list",
    "清单": "list",
    "时间轴": "timeline",
    "时间线": "timeline",
    "分栏": "columns",
    "多栏": "columns",
    "环形图": "donut",
    "饼图": "donut",
    "折线": "line",
    "折线图": "line",
    "柱状": "bar",
    "柱状图": "bar",
    "四格": "fitt",
    "作息条": "timebar",
    "圆点": "dots",
    "编号": "numbers",
    "打勾": "checks",
    "双栏": "two-column",
    "单栏": "one-column",
    "带底色": "boxed",
    "无底色": "plain",
}
```

- [ ] **Step 3: 写失败测试 `platform-skills/tests/health_plan_report/conftest.py` 与 `test_content.py`**

`test_content.py`（`--import-mode=importlib` 下测试模块不能 `import conftest`，路径在测试文件里直接重算）：

```python
import json
import subprocess
import sys
from pathlib import Path

from hpr.catalog import KINDS, VARIANTS
from hpr.content import ContentError, load_content, required_texts, validate_content

SKILL_DIR = Path(__file__).resolve().parents[2] / "health-plan-report"
SAMPLE = SKILL_DIR / "sample" / "sample-plan.json"
VALIDATE = SKILL_DIR / "scripts" / "validate.py"


def _paths(errs: list[ContentError]) -> list[str]:
    return [e.path for e in errs]


def test_sample_is_valid_and_covers_every_kind(sample):
    assert validate_content(sample) == []
    seen = {b["kind"] for s in sample["sections"] for b in s["blocks"]}
    assert seen == set(KINDS)


def test_kinds_and_variants_consistent():
    assert set(KINDS) == set(VARIANTS)
    assert all(VARIANTS[k] for k in KINDS)


def test_unknown_kind_reported_with_path(sample):
    sample["sections"][0]["blocks"][0] = {"kind": "medication", "items": []}
    errs = validate_content(sample)
    assert _paths(errs) == ["sections[0].blocks[0].kind"]


def test_missing_required_field(sample):
    del sample["title"]
    assert "title" in _paths(validate_content(sample))


def test_unsupported_field(sample):
    sample["sections"][0]["blocks"][0]["color"] = "red"
    assert "sections[0].blocks[0].color" in _paths(validate_content(sample))


def test_table_row_column_mismatch(sample):
    table = next(b for s in sample["sections"] for b in s["blocks"] if b["kind"] == "table")
    table["rows"].append(["only one cell"])
    errs = validate_content(sample)
    assert any(e.path.endswith(f"rows[{len(table['rows']) - 1}]") for e in errs)


def test_duplicate_section_id_and_bad_id(sample):
    sample["sections"][1]["id"] = sample["sections"][0]["id"]
    sample["sections"][2]["id"] = "Bad Id"
    paths = _paths(validate_content(sample))
    assert "sections[1].id" in paths
    assert "sections[2].id" in paths


def test_duplicate_block_id_within_section(sample):
    sec = next(s for s in sample["sections"] if len(s["blocks"]) >= 2)
    sec["blocks"][0]["id"] = "dup"
    sec["blocks"][1]["id"] = "dup"
    idx = sample["sections"].index(sec)
    assert f"sections[{idx}].blocks[1].id" in _paths(validate_content(sample))


def test_bad_date_url_hhmm(sample):
    sample["generated_at"] = "2026/09/28"
    mat = next(b for s in sample["sections"] for b in s["blocks"] if b["kind"] == "material")
    mat["url"] = "ftp://x"
    sleep = next(b for s in sample["sections"] for b in s["blocks"] if b["kind"] == "sleep")
    sleep["current"]["bed"] = "25:00"
    paths = _paths(validate_content(sample))
    assert "generated_at" in paths
    assert any(p.endswith(".url") for p in paths)
    assert any(p.endswith(".current.bed") for p in paths)


def test_trend_needs_two_numeric_points(sample):
    trend = next(b for s in sample["sections"] for b in s["blocks"] if b["kind"] == "trend")
    trend["points"] = [{"date": "2026-09-01", "value": "6.1"}]
    paths = _paths(validate_content(sample))
    assert any(p.endswith(".points") for p in paths)
    assert any(p.endswith(".points[0].value") for p in paths)


def test_any_of_rule(sample):
    rules = next(b for s in sample["sections"] for b in s["blocks"] if b["kind"] == "diet_rules")
    for k in ("recommend", "limit", "avoid", "swaps"):
        rules.pop(k, None)
    errs = validate_content(sample)
    assert any("至少需要以下字段之一" in e.message for e in errs)


def test_bool_is_not_a_number(sample):
    trend = next(b for s in sample["sections"] for b in s["blocks"] if b["kind"] == "trend")
    trend["points"][0]["value"] = True
    assert any(p.endswith(".points[0].value") for p in _paths(validate_content(sample)))


def test_required_texts_skip_ids_urls_and_chart_points(sample):
    texts = required_texts(sample)
    assert sample["title"] in texts
    assert sample["sections"][0]["title"] in texts
    assert sample["sections"][0]["id"] not in texts
    assert all(not t.startswith("http") for t in texts)
    trend = next(b for s in sample["sections"] for b in s["blocks"] if b["kind"] == "trend")
    assert trend["points"][0]["date"] not in texts


def test_load_content_rejects_non_object(tmp_path):
    p = tmp_path / "c.json"
    p.write_text("[1, 2]", encoding="utf-8")
    try:
        load_content(p)
    except ValueError as exc:
        assert "对象" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_validate_cli_exit_codes(tmp_path, sample):
    ok = subprocess.run(
        [sys.executable, str(VALIDATE), str(SAMPLE)], capture_output=True, text=True, check=False
    )
    assert ok.returncode == 0, ok.stderr
    assert json.loads(ok.stdout)["ok"] is True
    del sample["sections"]
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(sample, ensure_ascii=False), encoding="utf-8")
    res = subprocess.run(
        [sys.executable, str(VALIDATE), str(bad)], capture_output=True, text=True, check=False
    )
    assert res.returncode == 1
    assert json.loads(res.stdout)["errors"] == ["sections: 缺少必填字段"]
```

`conftest.py`：

```python
import copy
import json
import sys
from pathlib import Path

import pytest

_SKILL_DIR = Path(__file__).resolve().parents[2] / "health-plan-report"
_SCRIPTS = _SKILL_DIR / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))


@pytest.fixture
def sample() -> dict:
    path = _SKILL_DIR / "sample" / "sample-plan.json"
    return copy.deepcopy(json.loads(path.read_text(encoding="utf-8")))
```

- [ ] **Step 4: 运行，确认失败**

Run: `uv run --no-sync pytest platform-skills/tests/health_plan_report/test_content.py -q`
Expected: 收集失败 `ModuleNotFoundError: No module named 'hpr.content'`

- [ ] **Step 5: 写 `hpr/content.py`**

```python
"""Content JSON: schema, validation and the inventory of texts that must appear (spec §4)."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hpr.catalog import KINDS

ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
HHMM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
URL_RE = re.compile(r"^https?://\S+$")


@dataclass(frozen=True)
class ContentError:
    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}: {self.message}"


@dataclass(frozen=True)
class F:
    spec: Any
    required: bool = True


def opt(spec: Any) -> F:
    return F(spec, required=False)


@dataclass(frozen=True)
class Enum:
    values: tuple[str, ...]


@dataclass(frozen=True)
class ListOf:
    item: Any
    min_items: int = 0


@dataclass(frozen=True)
class Obj:
    fields: dict[str, F]
    any_of: tuple[str, ...] = ()


TEXT = "text"
NUMTEXT = "numtext"
NUM = "num"
DATE = "date"
URL = "url"
PATH = "path"
HHMM = "hhmm"
IDENT = "ident"
BLOCK = "block"

_KV = Obj({"label": F(TEXT), "value": F(TEXT)})
_BEDWAKE = Obj({"bed": F(HHMM), "wake": F(HHMM)})

KIND_SCHEMAS: dict[str, Obj] = {
    "summary": Obj({"items": F(ListOf(Obj({"label": F(TEXT), "text": F(TEXT)}), 1))}),
    "profile": Obj(
        {
            "items": F(
                ListOf(
                    Obj(
                        {
                            "name": F(TEXT),
                            "value": F(NUMTEXT),
                            "unit": opt(TEXT),
                            "ref_low": opt(NUM),
                            "ref_high": opt(NUM),
                            "ref_text": opt(TEXT),
                            "position": opt(Enum(("within", "above", "below", "none"))),
                        }
                    ),
                    1,
                )
            )
        }
    ),
    "issues": Obj(
        {
            "items": F(
                ListOf(
                    Obj(
                        {
                            "title": F(TEXT),
                            "evidence": opt(TEXT),
                            "level": opt(Enum(("focus", "watch", "info"))),
                        }
                    ),
                    1,
                )
            )
        }
    ),
    "trend": Obj(
        {
            "metric": F(TEXT),
            "unit": opt(TEXT),
            "points": F(ListOf(Obj({"date": F(TEXT), "value": F(NUM)}), 2)),
            "target_low": opt(NUM),
            "target_high": opt(NUM),
            "ref_text": opt(TEXT),
        }
    ),
    "goals": Obj(
        {
            "items": F(
                ListOf(
                    Obj(
                        {
                            "name": F(TEXT),
                            "current": opt(NUMTEXT),
                            "target": F(NUMTEXT),
                            "unit": opt(TEXT),
                            "due": opt(TEXT),
                            "note": opt(TEXT),
                        }
                    ),
                    1,
                )
            )
        }
    ),
    "phases": Obj(
        {
            "items": F(
                ListOf(
                    Obj(
                        {
                            "label": F(TEXT),
                            "focus": F(ListOf(Obj({"area": F(TEXT), "text": F(TEXT)}), 1)),
                        }
                    ),
                    1,
                )
            )
        }
    ),
    "nutrition": Obj(
        {
            "energy_kcal": opt(NUMTEXT),
            "macros": opt(
                ListOf(
                    Obj({"name": F(TEXT), "grams": opt(NUMTEXT), "percent": opt(NUMTEXT)}), 1
                )
            ),
            "meals": opt(
                ListOf(Obj({"name": F(TEXT), "percent": opt(NUMTEXT), "note": opt(TEXT)}), 1)
            ),
        },
        any_of=("energy_kcal", "macros", "meals"),
    ),
    "meal_plan": Obj(
        {
            "templates": F(
                ListOf(
                    Obj(
                        {
                            "name": opt(TEXT),
                            "applies_to": opt(TEXT),
                            "meals": F(
                                ListOf(
                                    Obj(
                                        {
                                            "name": F(TEXT),
                                            "time": opt(TEXT),
                                            "foods": F(
                                                ListOf(
                                                    Obj({"name": F(TEXT), "amount": opt(TEXT)}),
                                                    1,
                                                )
                                            ),
                                            "kcal": opt(NUMTEXT),
                                        }
                                    ),
                                    1,
                                )
                            ),
                        }
                    ),
                    1,
                )
            )
        }
    ),
    "diet_rules": Obj(
        {
            "recommend": opt(ListOf(TEXT, 1)),
            "limit": opt(ListOf(TEXT, 1)),
            "avoid": opt(ListOf(TEXT, 1)),
            "swaps": opt(ListOf(Obj({"from": F(TEXT), "to": F(TEXT)}), 1)),
        },
        any_of=("recommend", "limit", "avoid", "swaps"),
    ),
    "exercise": Obj(
        {
            "fitt": opt(
                Obj(
                    {
                        "frequency": opt(TEXT),
                        "intensity": opt(TEXT),
                        "time": opt(TEXT),
                        "type": opt(TEXT),
                    },
                    any_of=("frequency", "intensity", "time", "type"),
                )
            ),
            "schedule": opt(ListOf(Obj({"day": F(TEXT), "items": F(ListOf(TEXT, 1))}), 1)),
            "progression": opt(TEXT),
            "cautions": opt(ListOf(TEXT, 1)),
        },
        any_of=("fitt", "schedule", "progression", "cautions"),
    ),
    "sleep": Obj(
        {"current": opt(_BEDWAKE), "target": opt(_BEDWAKE), "tips": opt(ListOf(TEXT, 1))},
        any_of=("current", "target", "tips"),
    ),
    "stress": Obj(
        {
            "status": opt(TEXT),
            "methods": opt(
                ListOf(
                    Obj({"name": F(TEXT), "how": opt(TEXT), "frequency": opt(TEXT)}), 1
                )
            ),
        },
        any_of=("status", "methods"),
    ),
    "habits": Obj(
        {
            "items": F(
                ListOf(
                    Obj(
                        {
                            "name": F(TEXT),
                            "current": opt(TEXT),
                            "target": opt(TEXT),
                            "how": opt(TEXT),
                        }
                    ),
                    1,
                )
            )
        }
    ),
    "material": Obj(
        {"name": F(TEXT), "description": F(TEXT), "url": F(URL), "media_path": opt(PATH)}
    ),
    "monitoring": Obj(
        {
            "items": F(
                ListOf(
                    Obj(
                        {
                            "item": F(TEXT),
                            "frequency": opt(TEXT),
                            "timing": opt(TEXT),
                            "alert": opt(TEXT),
                        }
                    ),
                    1,
                )
            )
        }
    ),
    "referral": Obj({"text": F(TEXT)}),
    "shopping": Obj(
        {"groups": F(ListOf(Obj({"name": F(TEXT), "items": F(ListOf(TEXT, 1))}), 1))}
    ),
    "follow_up": Obj({"items": F(ListOf(_KV, 1))}),
    "paragraph": Obj({"text": F(TEXT)}),
    "bullets": Obj({"items": F(ListOf(TEXT, 1))}),
    "table": Obj({"columns": F(ListOf(TEXT, 1)), "rows": F(ListOf(ListOf(TEXT, 1), 1))}),
    "kv": Obj({"items": F(ListOf(_KV, 1))}),
    "image": Obj({"path": F(PATH), "caption": opt(TEXT)}),
    "callout": Obj(
        {"level": opt(Enum(("info", "warn"))), "title": opt(TEXT), "text": F(TEXT)}
    ),
}

SECTION = Obj({"id": F(IDENT), "title": F(TEXT), "blocks": F(ListOf(BLOCK, 1))})

TOP = Obj(
    {
        "schema_version": F(Enum(("1",))),
        "title": F(TEXT),
        "subtitle": opt(TEXT),
        "period": opt(Obj({"label": F(TEXT), "weeks": opt(NUM)})),
        "generated_at": F(DATE),
        "data_basis": opt(TEXT),
        "client": F(
            Obj(
                {
                    "name": F(TEXT),
                    "facts": opt(ListOf(Obj({"label": F(TEXT), "value": F(TEXT)}))),
                }
            )
        ),
        "manager": opt(Obj({"name": F(TEXT), "title": opt(TEXT)})),
        "brand": opt(
            Obj(
                {
                    "org_name": opt(TEXT),
                    "logo_path": opt(PATH),
                    "footer_signature": opt(TEXT),
                    "disclaimer": opt(TEXT),
                }
            )
        ),
        "sections": F(ListOf(SECTION, 1)),
    }
)

_SCALAR_MSG = {
    TEXT: "应为非空文字",
    NUMTEXT: "应为数字或非空文字",
    NUM: "应为数字",
    DATE: "应为 YYYY-MM-DD 日期",
    URL: "应为 http(s) 链接",
    PATH: "应为非空文件路径",
    HHMM: "应为 HH:MM 时间",
    IDENT: "应为小写字母开头、只含小写字母/数字/-/_ 的短标识（≤32）",
}


def _join(path: str, key: str) -> str:
    return f"{path}.{key}" if path else key


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _nonempty_str(v: Any) -> bool:
    return isinstance(v, str) and bool(v.strip())


def _scalar_ok(value: Any, spec: str) -> bool:
    if spec == TEXT or spec == PATH:
        return _nonempty_str(value)
    if spec == NUMTEXT:
        return _is_num(value) or _nonempty_str(value)
    if spec == NUM:
        return _is_num(value)
    if spec == DATE:
        return isinstance(value, str) and bool(DATE_RE.match(value))
    if spec == URL:
        return isinstance(value, str) and bool(URL_RE.match(value))
    if spec == HHMM:
        return isinstance(value, str) and bool(HHMM_RE.match(value))
    if spec == IDENT:
        return isinstance(value, str) and bool(ID_RE.match(value))
    raise AssertionError(f"unknown scalar spec {spec!r}")


def _check(value: Any, spec: Any, path: str, errs: list[ContentError]) -> None:
    if isinstance(spec, Obj):
        if not isinstance(value, dict):
            errs.append(ContentError(path, "应为对象"))
            return
        for key in value:
            if key not in spec.fields:
                errs.append(ContentError(_join(path, key), "不支持的字段"))
        for key, field in spec.fields.items():
            if key in value:
                _check(value[key], field.spec, _join(path, key), errs)
            elif field.required:
                errs.append(ContentError(_join(path, key), "缺少必填字段"))
        if spec.any_of and not any(k in value for k in spec.any_of):
            errs.append(ContentError(path, "至少需要以下字段之一：" + "、".join(spec.any_of)))
        return
    if isinstance(spec, ListOf):
        if not isinstance(value, list):
            errs.append(ContentError(path, "应为数组"))
            return
        if len(value) < spec.min_items:
            errs.append(ContentError(path, f"至少需要 {spec.min_items} 项"))
        for i, item in enumerate(value):
            _check(item, spec.item, f"{path}[{i}]", errs)
        return
    if isinstance(spec, Enum):
        if value not in spec.values:
            errs.append(ContentError(path, "取值应为：" + " / ".join(spec.values)))
        return
    if spec == BLOCK:
        _check_block(value, path, errs)
        return
    if not _scalar_ok(value, spec):
        errs.append(ContentError(path, _SCALAR_MSG[spec]))


def _check_block(block: Any, path: str, errs: list[ContentError]) -> None:
    if not isinstance(block, dict):
        errs.append(ContentError(path, "应为对象"))
        return
    kind = block.get("kind")
    if kind not in KINDS:
        errs.append(ContentError(_join(path, "kind"), "未知积木类型：" + str(kind)))
        return
    if "id" in block and not _scalar_ok(block["id"], IDENT):
        errs.append(ContentError(_join(path, "id"), _SCALAR_MSG[IDENT]))
    body = {k: v for k, v in block.items() if k not in ("kind", "id")}
    _check(body, KIND_SCHEMAS[kind], path, errs)
    if kind == "table" and isinstance(block.get("columns"), list):
        width = len(block["columns"])
        for i, row in enumerate(block.get("rows") or []):
            if isinstance(row, list) and len(row) != width:
                errs.append(ContentError(f"{path}.rows[{i}]", f"应有 {width} 列，实际 {len(row)} 列"))


def _check_ids(content: dict, errs: list[ContentError]) -> None:
    seen: set[str] = set()
    for si, sec in enumerate(content.get("sections") or []):
        if not isinstance(sec, dict):
            continue
        sid = sec.get("id")
        if isinstance(sid, str) and sid in seen:
            errs.append(ContentError(f"sections[{si}].id", f"章节 id 重复：{sid}"))
        if isinstance(sid, str):
            seen.add(sid)
        bseen: set[str] = set()
        for bi, blk in enumerate(sec.get("blocks") or []):
            bid = blk.get("id") if isinstance(blk, dict) else None
            if isinstance(bid, str) and bid in bseen:
                errs.append(ContentError(f"sections[{si}].blocks[{bi}].id", f"积木 id 重复：{bid}"))
            if isinstance(bid, str):
                bseen.add(bid)


def validate_content(content: Any) -> list[ContentError]:
    errs: list[ContentError] = []
    _check(content, TOP, "", errs)
    if isinstance(content, dict):
        _check_ids(content, errs)
    return errs


def load_content(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"内容文件读取失败或不是合法 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("内容文件的顶层必须是 JSON 对象")
    return data


_NON_TEXT_KEYS = frozenset(
    {"schema_version", "kind", "id", "url", "media_path", "logo_path", "path", "position", "level"}
)


def required_texts(content: dict) -> list[str]:
    """Every string the reader must find in the deliverable (numbers and chart points excluded)."""
    out: list[str] = []

    def walk(value: Any, in_points: bool) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key not in _NON_TEXT_KEYS:
                    walk(item, in_points or key == "points")
        elif isinstance(value, list):
            for item in value:
                walk(item, in_points)
        elif isinstance(value, str) and not in_points and value.strip():
            out.append(value)

    walk(content, False)
    return out


def iter_blocks(content: dict) -> Iterator[tuple[int, str, int, dict]]:
    for si, sec in enumerate(content["sections"]):
        for bi, blk in enumerate(sec["blocks"], start=1):
            yield si, sec["id"], bi, blk
```

- [ ] **Step 6: 写 `scripts/validate.py`**

```python
#!/usr/bin/env python3
"""Validate a health-plan content JSON. Exit 0 with {"ok": true}, or exit 1 with the error list."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _cli import emit_json, fail
from hpr.content import load_content, validate_content


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("content", help="内容 JSON 文件")
    args = ap.parse_args(argv)
    try:
        content = load_content(Path(args.content))
    except ValueError as exc:
        fail(str(exc))
    errs = validate_content(content)
    if errs:
        emit_json({"ok": False, "errors": [str(e) for e in errs]})
        return 1
    emit_json({"ok": True, "sections": len(content["sections"])})
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

`_cli.py` 由 `skill.yaml` 从 `shared/` 打包进 `scripts/`；在仓库源码树里运行（测试）时，`scripts/` 下没有 `_cli.py`。因此测试的 CLI 调用前要能找到它：在 `validate.py` 的 `sys.path.insert` 之后再加一行回落路径——

```python
sys.path.insert(1, str(Path(__file__).resolve().parents[2] / "shared"))
```

（打包后 `parents[2]` 是技能根的上一级，不存在 `shared/`，这一行无副作用；源码树里它指向 `platform-skills/shared/`。）`resolve_style.py`、`render.py` 同样加这两行。

- [ ] **Step 7: 写 `sample/sample-plan.json`（虚构样例，覆盖全部 24 种积木）**

```json
{
  "schema_version": "1",
  "title": "李先生健康管理方案",
  "subtitle": "第 1 阶段 · 2 周 ｜ 控糖与体重管理",
  "period": {"label": "第 1 阶段", "weeks": 2},
  "generated_at": "2026-09-28",
  "data_basis": "近 30 天记录",
  "client": {
    "name": "李先生",
    "facts": [
      {"label": "性别 / 年龄", "value": "男 · 48 岁"},
      {"label": "身高 / 体重", "value": "172 cm · 77.0 kg"},
      {"label": "管理方向", "value": "控糖 + 体重管理"}
    ]
  },
  "manager": {"name": "王老师", "title": "健康管理师"},
  "brand": {
    "org_name": "示例健康管理中心",
    "footer_signature": "健康管理团队",
    "disclaimer": "本方案为生活方式管理建议，不构成医学诊断或治疗建议；如有不适请及时就医。"
  },
  "sections": [
    {"id": "summary", "title": "一页看懂", "blocks": [
      {"kind": "summary", "items": [
        {"label": "核心问题", "text": "空腹血糖略高于参考范围，体重超出目标约 5 kg。"},
        {"label": "阶段目标", "text": "两周内空腹血糖回到 6.1 以下，体重下降 1.0–1.5 kg。"},
        {"label": "关键行动", "text": "控制总热量、餐后快走 30 分钟、每天记录血糖与体重。"}
      ]}
    ]},
    {"id": "profile", "title": "健康画像", "blocks": [
      {"kind": "profile", "items": [
        {"name": "空腹血糖", "value": 6.4, "unit": "mmol/L", "ref_low": 3.9, "ref_high": 6.1, "position": "above"},
        {"name": "BMI", "value": 26.0, "ref_low": 18.5, "ref_high": 23.9, "position": "above"},
        {"name": "体脂率", "value": 31.5, "unit": "%", "ref_low": 20, "ref_high": 30, "position": "above"},
        {"name": "血尿酸", "value": 398, "unit": "μmol/L", "ref_low": 208, "ref_high": 428, "position": "within"}
      ]},
      {"kind": "issues", "items": [
        {"title": "血糖偏高", "evidence": "近 30 天空腹血糖有 9 天高于 6.1", "level": "focus"},
        {"title": "体重超标", "evidence": "BMI 26.0，较目标高约 5 kg", "level": "focus"},
        {"title": "肌肉量偏低", "evidence": "骨骼肌 22.4 kg，减重期需保肌", "level": "watch"}
      ]}
    ]},
    {"id": "trend", "title": "近 30 天趋势", "blocks": [
      {"kind": "trend", "metric": "空腹血糖", "unit": "mmol/L", "target_low": 4.4, "target_high": 6.1,
       "ref_text": "虚线为本阶段目标区间。", "points": [
        {"date": "2026-08-30", "value": 6.8}, {"date": "2026-09-03", "value": 6.9},
        {"date": "2026-09-07", "value": 6.6}, {"date": "2026-09-11", "value": 6.7},
        {"date": "2026-09-15", "value": 6.5}, {"date": "2026-09-19", "value": 6.3},
        {"date": "2026-09-23", "value": 6.4}, {"date": "2026-09-27", "value": 6.2}
      ]},
      {"kind": "image", "path": "trend-note.png", "caption": "示例配图（虚构）"}
    ]},
    {"id": "goals", "title": "阶段目标与计划", "blocks": [
      {"kind": "goals", "items": [
        {"name": "空腹血糖", "current": 6.4, "target": "< 6.1", "unit": "mmol/L", "due": "2 周"},
        {"name": "体重", "current": 77.0, "target": "75.5–76.0", "unit": "kg", "due": "2 周", "note": "每周下降 0.5–0.8 kg"}
      ]},
      {"kind": "phases", "items": [
        {"label": "第 1 周", "focus": [{"area": "饮食", "text": "按 A/B 模板执行，记录三餐"}, {"area": "运动", "text": "餐后快走 25 分钟，每周 5 次"}]},
        {"label": "第 2 周", "focus": [{"area": "饮食", "text": "复盘餐后血糖，调整进食顺序"}, {"area": "运动", "text": "快走 30 分钟，加入 2 次抗阻"}]}
      ]}
    ]},
    {"id": "diet", "title": "饮食安排", "blocks": [
      {"kind": "nutrition", "energy_kcal": 1650, "macros": [
        {"name": "碳水化合物", "grams": 185, "percent": 45},
        {"name": "蛋白质", "grams": 95, "percent": 23},
        {"name": "脂肪", "grams": 58, "percent": 32}
      ], "meals": [
        {"name": "早餐", "percent": 25}, {"name": "午餐", "percent": 35, "note": "主食粗细搭配"},
        {"name": "晚餐", "percent": 30}, {"name": "加餐", "percent": 10, "note": "水果或坚果"}
      ]},
      {"kind": "meal_plan", "templates": [
        {"name": "A 模板", "applies_to": "周一 / 三 / 五 / 日", "meals": [
          {"name": "早餐", "time": "7:30", "foods": [{"name": "杂粮馒头", "amount": "50 g"}, {"name": "鸡蛋", "amount": "1 个"}, {"name": "无糖豆浆", "amount": "250 ml"}], "kcal": 420},
          {"name": "午餐", "time": "12:00", "foods": [{"name": "杂粮饭", "amount": "75 g"}, {"name": "去皮鸡腿肉", "amount": "100 g"}, {"name": "清炒时蔬", "amount": "200 g"}], "kcal": 580},
          {"name": "晚餐", "time": "18:30", "foods": [{"name": "荞麦面", "amount": "50 g"}, {"name": "清蒸鱼", "amount": "100 g"}, {"name": "蔬菜", "amount": "200 g"}], "kcal": 480}
        ]}
      ]},
      {"kind": "diet_rules", "recommend": ["粗细搭配的主食", "每餐先吃蔬菜"], "limit": ["水果每天不超过 200 g"], "avoid": ["含糖饮料", "动物内脏、浓肉汤"], "swaps": [{"from": "白米饭", "to": "杂粮饭"}]},
      {"kind": "shopping", "groups": [
        {"name": "主食", "items": ["燕麦", "荞麦面", "杂粮米"]},
        {"name": "蛋白质", "items": ["鸡蛋", "鸡腿肉", "鱼", "豆腐"]},
        {"name": "蔬果", "items": ["西兰花", "黄瓜", "苹果"]}
      ]}
    ]},
    {"id": "exercise", "title": "运动安排", "blocks": [
      {"kind": "exercise", "fitt": {"frequency": "每周 5 次", "intensity": "中等，微微出汗能说话", "time": "每次 30 分钟", "type": "餐后快走 + 抗阻"},
       "schedule": [{"day": "周一 / 三 / 五", "items": ["快走 30 分钟"]}, {"day": "周二 / 四", "items": ["靠墙静蹲 3 组", "弹力带划船 3 组"]}],
       "progression": "第 2 周起快走延长到 35 分钟。", "cautions": ["运动中头晕、心慌立即停止", "空腹时不做中等以上强度运动"]},
      {"kind": "material", "name": "快走动作示范", "description": "示范视频说明快走姿势与呼吸节奏（虚构素材）。", "url": "https://example.com/materials/brisk-walk"}
    ]},
    {"id": "lifestyle", "title": "睡眠与生活方式", "blocks": [
      {"kind": "sleep", "current": {"bed": "00:30", "wake": "07:00"}, "target": {"bed": "23:00", "wake": "06:30"}, "tips": ["睡前 1 小时不看手机", "午睡不超过 30 分钟"]},
      {"kind": "stress", "status": "工作压力较大，晚间易焦虑。", "methods": [{"name": "腹式呼吸", "how": "吸 4 秒、呼 6 秒", "frequency": "每天 2 次"}, {"name": "晚间散步", "frequency": "每周 3 次"}]},
      {"kind": "habits", "items": [{"name": "饮酒", "current": "每周 3 次", "target": "每周不超过 1 次", "how": "聚餐改喝无糖茶"}, {"name": "久坐", "current": "连续坐 3 小时", "target": "每小时起身 5 分钟"}]}
    ]},
    {"id": "monitoring", "title": "监测计划", "blocks": [
      {"kind": "monitoring", "items": [
        {"item": "空腹血糖", "frequency": "每周 4 次", "timing": "晨起空腹", "alert": "≥ 7.0 连续 2 次请联系管理师"},
        {"item": "体重", "frequency": "每天 1 次", "timing": "晨起排便后"},
        {"item": "腰围", "frequency": "每周 1 次"}
      ]},
      {"kind": "table", "columns": ["日期", "空腹血糖", "体重", "备注"], "rows": [["第 1 天", "—", "—", "—"], ["第 2 天", "—", "—", "—"]]}
    ]},
    {"id": "notes", "title": "注意事项", "blocks": [
      {"kind": "referral", "text": "如出现持续口渴多尿、视物模糊等情况，建议先就医确认。"},
      {"kind": "callout", "level": "warn", "title": "提醒", "text": "方案执行中如有不适请暂停并联系健康管理师。"},
      {"kind": "bullets", "items": ["按时记录数据", "两周后复盘调整"]},
      {"kind": "paragraph", "text": "家人可协助准备清淡餐食，提醒按时运动。"},
      {"kind": "kv", "items": [{"label": "反馈方式", "value": "企业微信"}, {"label": "响应时间", "value": "工作日 24 小时内"}]},
      {"kind": "follow_up", "items": [{"label": "复评日期", "value": "2026-10-12"}, {"label": "负责管理师", "value": "王老师"}]}
    ]}
  ]
}
```

`image` 积木引用的 `trend-note.png` 由 T5 测试在临时目录里现场生成，样例不打包图片（技能包只能含文本文件）。

- [ ] **Step 8: 调整平台检查测试**

`platform-skills/tests/conftest.py:8`：`EXPECTED_SKILLS = ("docx", "pptx", "xlsx", "pdf")` 改为：

```python
ALL_SKILLS = ("docx", "pptx", "xlsx", "pdf", "health-plan-report")
```

（`grep -rn "EXPECTED_SKILLS" platform-skills/tests` 确认 conftest 里这个常量没有其它使用者；若有，改用 `ALL_SKILLS`。）

`platform-skills/tests/test_platform_checks.py`：把第 25 行常量改名 `OFFICE_SKILLS`，文件内全部 `EXPECTED_SKILLS` 替换为 `OFFICE_SKILLS`，并把总数断言改为：

```python
def test_exactly_the_expected_skills_exist(built_packages):
    assert sorted(built_packages) == sorted((*OFFICE_SKILLS, "health-plan-report"))
```

（删除原 `test_exactly_the_four_office_skills_exist`。其余测试仍只参数化 office 四件套；新技能的包级检查在 T9。）

- [ ] **Step 9: 运行测试**

Run: `uv run --no-sync pytest platform-skills/tests -q`
Expected: 全部 PASS（`test_in_image.py` 因未设镜像环境变量 skip）。

break → red → restore → green 自证（逐条做、逐条还原）：
- 把 `_check_ids` 的块内重复判断注释掉 → `test_duplicate_block_id_within_section` 红；还原。
- 把 `required_texts` 里的 `in_points or key == "points"` 改成 `in_points` → `test_required_texts_skip_ids_urls_and_chart_points` 红；还原。
- 把 `_is_num` 的 `and not isinstance(v, bool)` 删掉 → `test_bool_is_not_a_number` 红；还原。

- [ ] **Step 10: lint 与提交**

```bash
uv run ruff check platform-skills && uv run ruff format --check platform-skills
git add platform-skills/health-plan-report platform-skills/tests/health_plan_report platform-skills/tests/test_platform_checks.py platform-skills/tests/conftest.py
git commit -m "feat(health-plan-report): skill skeleton + content JSON schema and validator"
```

---
### Task 2: 样式层解析（叠加、换算、品牌锁定、可读性纠正、参数落实清单）

**Files:**
- Create: `platform-skills/health-plan-report/scripts/hpr/style.py`
- Create: `platform-skills/health-plan-report/scripts/resolve_style.py`
- Create: `platform-skills/tests/health_plan_report/test_style.py`
- Modify: `docs/superpowers/specs/2026-09-28-health-plan-report-skill-design.md` §5.1「积木版式」行（按裁定 R1 改写定位键）

**Interfaces:**
- Consumes: `hpr.catalog.VARIANTS`、`VARIANT_SYNONYMS`；`hpr.content.iter_blocks`
- Produces:
  - `hpr.style.Layer(name: str, values: dict[str, object])`；`load_layer(path: Path) -> Layer`（非 JSON / 非对象 → `ValueError`）；`flatten(d: dict) -> dict[str, object]`
  - `hpr.style.Resolution(style: dict[str, object], report: list[dict])`；`resolve(layers: list[Layer], content: dict | None = None) -> Resolution`（`layers` 由高到低）
  - `style` 键：`OPTIONS` 全部键（值已标准化：颜色 `#RRGGBB`，布尔 `bool`，枚举英文名）+ 版式键 `blocks.<kind>.variant` / `sections.<sid>.variant` / `sections.<sid>.<bid>.variant`
  - `hpr.style.variant_for(style: dict, kind: str, section_id: str, block_id: str | None, index: int) -> str`
  - `hpr.style.contrast(a: str, b: str) -> float`、`luminance(hex: str) -> float`、`mix(a: str, b: str, t: float) -> str`（t=0 → a，t=1 → b）
  - `report` 每项：`{"key", "layer", "input", "value", "status", "note"}`，`status ∈ applied | adjusted | overridden | out_of_range | brand_locked`
  - `NOT_APPLIED = ("adjusted", "out_of_range", "brand_locked")`（Agent 必须告知用户的状态）
  - CLI：
    - `resolve_style.py --style A.json [--style B.json ...] [--content C.json]` → stdout `{"style": {...}, "report": [...]}`，退出 0；层文件坏 → 退出 1
    - `resolve_style.py --merge-into report-style/personal.json --set 键=值 [--set ...] [--unset 键 ...]` → 校验后写入；任一项非 applied/adjusted → 退出 1 且不写文件

- [ ] **Step 1: 写失败测试 `test_style.py`**

```python
import json
import subprocess
import sys
from pathlib import Path

import pytest
from hpr.style import (
    NOT_APPLIED,
    Layer,
    contrast,
    flatten,
    load_layer,
    luminance,
    resolve,
    variant_for,
)

SCRIPT = Path(__file__).resolve().parents[2] / "health-plan-report" / "scripts" / "resolve_style.py"


def _entry(res, key, layer=None):
    return next(e for e in res.report if e["key"] == key and (layer is None or e["layer"] == layer))


def test_defaults_are_direction_a():
    s = resolve([]).style
    assert s["color.primary"] == "#0B4F5C"
    assert s["color.accent"] == "#E8A33D"
    assert s["color.background"] == "#FFFFFF"
    assert s["type.scale"] == "standard"
    assert s["cover.variant"] == "band"
    assert s["output.formats"] == "pptx"


def test_higher_layer_wins_and_lower_is_overridden():
    res = resolve([Layer("本次", {"color.primary": "墨绿"}), Layer("个人", {"color.primary": "#1E3A8A"})])
    assert res.style["color.primary"] == "#1F4D3A"
    assert _entry(res, "color.primary", "本次")["status"] == "applied"
    assert _entry(res, "color.primary", "个人")["status"] == "overridden"


def test_chinese_synonyms():
    res = resolve([Layer("x", {"brand.logo_position": "右上角", "footer.align": "居中", "toc": "不要"})])
    assert res.style["brand.logo_position"] == "top-right"
    assert res.style["footer.align"] == "center"
    assert res.style["toc"] == "off"


def test_color_suffix_se_is_tolerated():
    assert resolve([Layer("x", {"color.primary": "深蓝色"})]).style["color.primary"] == "#1E3A8A"


def test_out_of_range_does_not_block_lower_layer():
    res = resolve([Layer("本次", {"type.scale": "巨大"}), Layer("个人", {"type.scale": "large"})])
    assert res.style["type.scale"] == "large"
    assert _entry(res, "type.scale", "本次")["status"] == "out_of_range"
    assert _entry(res, "type.scale", "个人")["status"] == "applied"


def test_unknown_key_reported():
    res = resolve([Layer("x", {"background.image": "sea.png"})])
    assert _entry(res, "background.image")["status"] == "out_of_range"


def test_brand_keys_are_locked():
    res = resolve([Layer("x", {"brand.org_name": "别的机构", "brand.disclaimer": "无"})])
    assert _entry(res, "brand.org_name")["status"] == "brand_locked"
    assert "brand.org_name" not in res.style


def test_dark_background_is_lightened():
    res = resolve([Layer("x", {"color.background": "#123456"})])
    bg = res.style["color.background"]
    assert luminance(bg) >= 0.80
    assert _entry(res, "color.background")["status"] == "adjusted"


def test_background_keywords():
    assert resolve([Layer("x", {"color.background": "浅灰"})]).style["color.background"] == "#F5F7F8"
    tint = resolve([Layer("x", {"color.background": "tint"})]).style["color.background"]
    assert luminance(tint) >= 0.80
    assert tint != "#FFFFFF"


def test_low_contrast_primary_is_darkened():
    res = resolve([Layer("x", {"color.primary": "浅蓝"})])
    p = res.style["color.primary"]
    assert contrast(p, res.style["color.background"]) >= 4.5
    assert _entry(res, "color.primary")["status"] == "adjusted"


def test_relative_words_move_one_step_from_lower_layer():
    res = resolve([Layer("本次", {"type.scale": "大一点"}), Layer("个人", {"type.scale": "compact"})])
    assert res.style["type.scale"] == "standard"
    res2 = resolve([Layer("本次", {"layout.density": "紧凑一点"})])
    assert res2.style["layout.density"] == "compact"
    res3 = resolve([Layer("本次", {"type.scale": "大一点"}), Layer("个人", {"type.scale": "large"})])
    assert res3.style["type.scale"] == "large"
    assert _entry(res3, "type.scale", "本次")["status"] == "out_of_range"


def test_nested_layer_is_flattened(tmp_path):
    p = tmp_path / "l.json"
    p.write_text(json.dumps({"color": {"primary": "深蓝"}, "type.scale": "large"}), encoding="utf-8")
    layer = load_layer(p)
    assert layer.values == {"color.primary": "深蓝", "type.scale": "large"}
    assert flatten({"a": {"b": {"c": 1}}}) == {"a.b.c": 1}


def test_non_object_layer_rejected(tmp_path):
    p = tmp_path / "l.json"
    p.write_text("[1]", encoding="utf-8")
    with pytest.raises(ValueError, match="JSON 对象"):
        load_layer(p)
    p.write_text("{oops", encoding="utf-8")
    with pytest.raises(ValueError, match="JSON"):
        load_layer(p)


def test_variant_keys_by_kind_section_and_block(sample):
    layers = [
        Layer(
            "x",
            {
                "blocks.meal_plan.variant": "表格",
                "sections.profile.variant": "table",
                "sections.diet.2.variant": "cards",
            },
        )
    ]
    res = resolve(layers, sample)
    s = res.style
    assert variant_for(s, "meal_plan", "diet", None, 2) == "cards"
    assert variant_for(s, "meal_plan", "other", None, 1) == "table"
    assert variant_for(s, "profile", "profile", None, 1) == "table"
    assert variant_for(s, "issues", "profile", None, 2) == "cards"  # issues 支持 cards 但不支持 table
    assert variant_for(s, "trend", "trend", None, 1) == "line"


def test_variant_targets_validated_against_content(sample):
    res = resolve(
        [Layer("x", {"sections.nope.variant": "table", "blocks.trend.variant": "table"})], sample
    )
    assert _entry(res, "sections.nope.variant")["status"] == "out_of_range"
    assert _entry(res, "blocks.trend.variant")["status"] == "out_of_range"


def test_section_variant_unsupported_by_every_block_is_out_of_range(sample):
    res = resolve([Layer("x", {"sections.trend.variant": "donut"})], sample)
    assert _entry(res, "sections.trend.variant")["status"] == "out_of_range"


def test_not_applied_statuses():
    assert set(NOT_APPLIED) == {"adjusted", "out_of_range", "brand_locked"}


def test_cli_resolve_and_merge(tmp_path):
    layer = tmp_path / "a.json"
    layer.write_text(json.dumps({"color.primary": "深蓝"}), encoding="utf-8")
    out = subprocess.run(
        [sys.executable, str(SCRIPT), "--style", str(layer)], capture_output=True, text=True, check=False
    )
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout)["style"]["color.primary"] == "#1E3A8A"

    personal = tmp_path / "report-style" / "personal.json"
    ok = subprocess.run(
        [sys.executable, str(SCRIPT), "--merge-into", str(personal), "--set", "type.scale=大号", "--set", "footer.page_number=false"],
        capture_output=True, text=True, check=False,
    )
    assert ok.returncode == 0, ok.stderr
    assert json.loads(personal.read_text(encoding="utf-8")) == {"footer.page_number": False, "type.scale": "大号"}

    before = personal.read_bytes()
    bad = subprocess.run(
        [sys.executable, str(SCRIPT), "--merge-into", str(personal), "--set", "type.scale=巨大"],
        capture_output=True, text=True, check=False,
    )
    assert bad.returncode == 1
    assert personal.read_bytes() == before

    unset = subprocess.run(
        [sys.executable, str(SCRIPT), "--merge-into", str(personal), "--unset", "type.scale"],
        capture_output=True, text=True, check=False,
    )
    assert unset.returncode == 0
    assert json.loads(personal.read_text(encoding="utf-8")) == {"footer.page_number": False}
```

说明：个人默认文件里保存的是**用户原话值**（如「大号」），每次渲染时再换算——这样换算表升级后老偏好自动受益；`--merge-into` 只负责校验「能换算」。

- [ ] **Step 2: 运行，确认失败**

Run: `uv run --no-sync pytest platform-skills/tests/health_plan_report/test_style.py -q`
Expected: `ModuleNotFoundError: No module named 'hpr.style'`

- [ ] **Step 3: 写 `hpr/style.py`**

```python
"""Style layers: merge high→low over platform defaults, normalise plain-language values,
lock brand keys, keep text readable, and account for every input (spec §5)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hpr.catalog import VARIANT_SYNONYMS, VARIANTS

POSITIONS = ("top-left", "top-right", "bottom-left", "bottom-right", "center")

#: key -> (kind, default). kind: "color" | "background" | "bool" | tuple(enum values)
OPTIONS: dict[str, tuple[Any, Any]] = {
    "color.primary": ("color", "#0B4F5C"),
    "color.accent": ("color", "#E8A33D"),
    "color.background": ("background", "#FFFFFF"),
    "type.scale": (("compact", "standard", "large"), "standard"),
    "layout.density": (("compact", "standard", "airy"), "standard"),
    "brand.logo_position": (POSITIONS, "top-left"),
    "brand.org_position": (POSITIONS, "top-left"),
    "footer.align": (("left", "center"), "left"),
    "footer.page_number": ("bool", True),
    "cover.variant": (("band", "split", "minimal"), "band"),
    "toc": (("auto", "on", "off"), "auto"),
    "section.icons": ("bool", True),
    "output.formats": (("pptx", "pdf", "both"), "pptx"),
}

BRAND_LOCKED = frozenset(
    {"brand.org_name", "brand.logo_path", "brand.footer_signature", "brand.disclaimer"}
)
NOT_APPLIED = ("adjusted", "out_of_range", "brand_locked")
INK = "#1D2B30"

_POS_SYN = {
    "左上": "top-left", "左上角": "top-left", "右上": "top-right", "右上角": "top-right",
    "左下": "bottom-left", "左下角": "bottom-left", "右下": "bottom-right", "右下角": "bottom-right",
    "居中": "center", "中间": "center", "正中": "center", "中央": "center",
}
ENUM_SYN: dict[str, dict[str, str]] = {
    "type.scale": {"紧凑": "compact", "小": "compact", "小号": "compact", "标准": "standard",
                   "默认": "standard", "正常": "standard", "大": "large", "大号": "large",
                   "大字": "large", "老年": "large", "适合老年人": "large"},
    "layout.density": {"紧凑": "compact", "紧密": "compact", "标准": "standard", "默认": "standard",
                       "宽松": "airy", "舒展": "airy", "留白多": "airy"},
    "brand.logo_position": _POS_SYN,
    "brand.org_position": _POS_SYN,
    "footer.align": {"左": "left", "靠左": "left", "左对齐": "left", "居中": "center", "中间": "center"},
    "cover.variant": {"色块": "band", "整版": "band", "整版色块": "band", "分栏": "split",
                      "左右": "split", "左右分栏": "split", "极简": "minimal", "简洁": "minimal",
                      "白底": "minimal"},
    "toc": {"自动": "auto", "要": "on", "开": "on", "显示": "on", "加目录": "on", "不要": "off",
            "关": "off", "隐藏": "off", "不要目录": "off"},
    "output.formats": {"ppt": "pptx", "幻灯片": "pptx", "演示文稿": "pptx", "都要": "both",
                       "两个都要": "both", "两种": "both"},
}
ORDERED: dict[str, tuple[str, ...]] = {
    "type.scale": ("compact", "standard", "large"),
    "layout.density": ("compact", "standard", "airy"),
}
UP_WORDS = frozenset({"大一点", "更大", "调大", "大些", "放大", "再大点", "宽松一点", "更宽松",
                      "松一点", "up", "larger", "bigger"})
DOWN_WORDS = frozenset({"小一点", "更小", "调小", "小些", "缩小", "再小点", "紧凑一点", "更紧凑",
                        "紧一点", "down", "smaller"})
_BOOL_SYN = {"开": True, "打开": True, "显示": True, "要": True, "是": True, "on": True,
             "true": True, "yes": True, "关": False, "关闭": False, "隐藏": False, "不要": False,
             "否": False, "off": False, "false": False, "no": False}

COLOR_NAMES: dict[str, str] = {
    "深青": "#0B4F5C", "青色": "#0E7C86", "青绿": "#11807A", "蓝": "#1F5BD8", "蓝色": "#1F5BD8",
    "深蓝": "#1E3A8A", "藏青": "#1C2640", "海军蓝": "#1C2640", "天蓝": "#3B82C4",
    "浅蓝": "#DCEBFA", "绿": "#2E7D5B", "绿色": "#2E7D5B", "墨绿": "#1F4D3A", "深绿": "#1E5B45",
    "浅绿": "#E3F2E9", "紫": "#5B3F9E", "紫色": "#5B3F9E", "深紫": "#3F2A73", "浅紫": "#ECE6F7",
    "红": "#B42318", "红色": "#B42318", "酒红": "#7A1F2B", "橙": "#D97706", "橙色": "#D97706",
    "暖橙": "#E8A33D", "金": "#C9A45C", "金色": "#C9A45C", "暖金": "#D2BD8F", "灰": "#6B7280",
    "灰色": "#6B7280", "深灰": "#374151", "浅灰": "#F3F4F6", "米白": "#FAF7F0", "米色": "#F5EFE3",
    "白": "#FFFFFF", "白色": "#FFFFFF", "黑": "#111111", "黑色": "#111111", "粉": "#F4D6DC",
    "粉色": "#F4D6DC", "浅粉": "#FBEAEE", "棕": "#7C4A2D", "棕色": "#7C4A2D", "咖啡色": "#6F4E37",
}
_BG_KEYWORDS = {"white": "#FFFFFF", "白": "#FFFFFF", "白色": "#FFFFFF", "纯白": "#FFFFFF",
                "light-gray": "#F5F7F8", "浅灰": "#F5F7F8", "浅灰色": "#F5F7F8"}
_TINT_WORDS = frozenset({"tint", "浅色调", "主色浅调", "主色浅色"})
_HEX6 = re.compile(r"^#[0-9A-Fa-f]{6}$")
_HEX3 = re.compile(r"^#[0-9A-Fa-f]{3}$")
_RGB = re.compile(r"^rgb\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})\s*\)$", re.I)
_VARIANT_KEY = re.compile(r"^blocks\.([a-z_]+)\.variant$")
_SECTION_KEY = re.compile(r"^sections\.([a-z][a-z0-9_-]{0,31})(?:\.([a-z0-9_-]{1,32}))?\.variant$")


@dataclass(frozen=True)
class Layer:
    name: str
    values: dict[str, Any]


@dataclass
class Resolution:
    style: dict[str, Any]
    report: list[dict[str, Any]]


# ---------- colour maths ----------

def _rgb(hex_: str) -> tuple[int, int, int]:
    return int(hex_[1:3], 16), int(hex_[3:5], 16), int(hex_[5:7], 16)


def _hex(r: float, g: float, b: float) -> str:
    return "#{:02X}{:02X}{:02X}".format(*(max(0, min(255, round(c))) for c in (r, g, b)))


def mix(a: str, b: str, t: float) -> str:
    ra, rb = _rgb(a), _rgb(b)
    return _hex(*(x * (1 - t) + y * t for x, y in zip(ra, rb, strict=True)))


def luminance(hex_: str) -> float:
    def lin(c: int) -> float:
        v = c / 255
        return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4

    r, g, b = (lin(c) for c in _rgb(hex_))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def parse_color(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    v = value.strip()
    if _HEX6.match(v):
        return v.upper()
    if _HEX3.match(v):
        return ("#" + "".join(ch * 2 for ch in v[1:])).upper()
    m = _RGB.match(v)
    if m:
        parts = [int(x) for x in m.groups()]
        return _hex(*parts) if all(p <= 255 for p in parts) else None
    if v in COLOR_NAMES:
        return COLOR_NAMES[v]
    if v.endswith("色") and v[:-1] in COLOR_NAMES:
        return COLOR_NAMES[v[:-1]]
    return None


# ---------- layers ----------

def flatten(d: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in d.items():
        full = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict):
            out.update(flatten(value, full))
        else:
            out[full] = value
    return out


def load_layer(path: Path) -> Layer:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"样式文件 {path.name} 读取失败或不是合法 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"样式文件 {path.name} 的顶层必须是 JSON 对象")
    return Layer(path.name, flatten(data))


# ---------- normalisation ----------

def _norm_enum(key: str, allowed: tuple[str, ...], raw: Any, below: Any) -> tuple[Any, str]:
    """Return (value, error_note). value None = rejected."""
    if not isinstance(raw, str):
        return None, "应为文字"
    v = raw.strip()
    if key in ORDERED and (v in UP_WORDS or v in DOWN_WORDS):
        order = ORDERED[key]
        i = order.index(below) + (1 if v in UP_WORDS else -1)
        if 0 <= i < len(order):
            return order[i], ""
        return None, "已经是最" + ("大" if v in UP_WORDS else "小") + "一档"
    if v.lower() in allowed:
        return v.lower(), ""
    syn = ENUM_SYN.get(key, {})
    if v in syn:
        return syn[v], ""
    if key == "output.formats" and v.lower() in ("ppt", "pdf"):
        return {"ppt": "pptx", "pdf": "pdf"}[v.lower()], ""
    return None, "可选值：" + " / ".join(allowed)


def _norm_bool(raw: Any) -> tuple[Any, str]:
    if isinstance(raw, bool):
        return raw, ""
    if isinstance(raw, str) and raw.strip().lower() in _BOOL_SYN:
        return _BOOL_SYN[raw.strip().lower()], ""
    return None, "应为 开 / 关"


def _norm_variant(raw: Any) -> str | None:
    if not isinstance(raw, str):
        return None
    v = raw.strip()
    return VARIANT_SYNONYMS.get(v, v.lower())


def _section_kinds(content: dict | None, sid: str, bid: str | None) -> list[str] | None:
    if content is None:
        return None
    for sec in content.get("sections", []):
        if sec.get("id") != sid:
            continue
        blocks = sec.get("blocks", [])
        if bid is None:
            return [b.get("kind") for b in blocks]
        for i, blk in enumerate(blocks, start=1):
            if blk.get("id") == bid or str(i) == bid:
                return [blk.get("kind")]
        return []
    return []


def _normalise(key: str, raw: Any, current: dict[str, Any], content: dict | None) -> tuple[Any, str]:
    if key in OPTIONS:
        kind, _default = OPTIONS[key]
        if kind == "color":
            c = parse_color(raw)
            return (c, "") if c else (None, "无法识别的颜色（可写 #RRGGBB 或常用中文色名）")
        if kind == "background":
            if isinstance(raw, str) and raw.strip() in _TINT_WORDS:
                return "tint", ""
            if isinstance(raw, str) and raw.strip().lower() in _BG_KEYWORDS:
                return _BG_KEYWORDS[raw.strip().lower()], ""
            c = parse_color(raw)
            return (c, "") if c else (None, "无法识别的背景色")
        if kind == "bool":
            return _norm_bool(raw)
        return _norm_enum(key, kind, raw, current[key])
    m = _VARIANT_KEY.match(key)
    if m:
        kind_name = m.group(1)
        v = _norm_variant(raw)
        if kind_name not in VARIANTS:
            return None, "未知积木类型"
        if v not in VARIANTS[kind_name]:
            return None, "可选版式：" + " / ".join(VARIANTS[kind_name])
        return v, ""
    m = _SECTION_KEY.match(key)
    if m:
        v = _norm_variant(raw)
        kinds = _section_kinds(content, m.group(1), m.group(2))
        if kinds is None:
            return v, ""
        if not kinds:
            return None, "内容里找不到这个章节或积木"
        if not any(v in VARIANTS.get(k, ()) for k in kinds):
            return None, "该处积木不支持这个版式"
        return v, ""
    return None, "未知可调项"


def _fix_background(bg: str, primary: str) -> tuple[str, str]:
    if bg == "tint":
        return mix(primary, "#FFFFFF", 0.94), ""
    if luminance(bg) >= 0.80:
        return bg, ""
    fixed = bg
    for step in range(1, 21):
        fixed = mix(bg, "#FFFFFF", step * 0.05)
        if luminance(fixed) >= 0.85:
            break
    return fixed, f"背景只支持浅色，已调浅为 {fixed}"


def _fix_primary(primary: str, bg: str) -> tuple[str, str]:
    if contrast(primary, bg) >= 4.5:
        return primary, ""
    fixed = primary
    for step in range(1, 21):
        fixed = mix(primary, "#000000", step * 0.05)
        if contrast(fixed, bg) >= 4.5:
            break
    return fixed, f"与背景对比不足，已加深为 {fixed}"


def resolve(layers: list[Layer], content: dict | None = None) -> Resolution:
    style: dict[str, Any] = {k: default for k, (_kind, default) in OPTIONS.items()}
    report: list[dict[str, Any]] = []
    final_entry: dict[str, dict[str, Any]] = {}
    for layer in reversed(layers):  # lowest first; higher layers overwrite
        for key, raw in layer.values.items():
            entry = {"key": key, "layer": layer.name, "input": raw, "value": None,
                     "status": "applied", "note": ""}
            report.append(entry)
            if key in BRAND_LOCKED:
                entry.update(status="brand_locked", note="机构品牌项只取内容里的 brand，不可由样式覆盖")
                continue
            value, note = _normalise(key, raw, style, content)
            if value is None:
                entry.update(status="out_of_range", note=note)
                continue
            entry["value"] = value
            style[key] = value
            if key in final_entry:
                final_entry[key]["status"] = "overridden"
                final_entry[key]["note"] = f"被更高一层「{layer.name}」覆盖"
            final_entry[key] = entry
    bg, bg_note = _fix_background(style["color.background"], style["color.primary"])
    style["color.background"] = bg
    if bg_note and "color.background" in final_entry:
        final_entry["color.background"].update(status="adjusted", value=bg, note=bg_note)
    primary, p_note = _fix_primary(style["color.primary"], bg)
    style["color.primary"] = primary
    if p_note and "color.primary" in final_entry:
        final_entry["color.primary"].update(status="adjusted", value=primary, note=p_note)
    return Resolution(style, report)


def variant_for(style: dict[str, Any], kind: str, section_id: str, block_id: str | None,
                index: int) -> str:
    allowed = VARIANTS[kind]
    for key in (
        f"sections.{section_id}.{block_id}.variant" if block_id else None,
        f"sections.{section_id}.{index}.variant",
        f"sections.{section_id}.variant",
        f"blocks.{kind}.variant",
    ):
        if key and style.get(key) in allowed:
            return style[key]
    return allowed[0]
```

注意 `test_relative_words_move_one_step_from_lower_layer` 的第三个断言：`large` 已是最大档，「大一点」→ `out_of_range`（note「已经是最大一档」），生效值保持 `large`。

- [ ] **Step 4: 写 `scripts/resolve_style.py`**

```python
#!/usr/bin/env python3
"""Resolve style layers (highest first) into the effective style + a parameter report,
or merge validated keys into a personal-default style file."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(1, str(Path(__file__).resolve().parents[2] / "shared"))

from _cli import emit_json, fail
from hpr.content import load_content
from hpr.style import Layer, load_layer, resolve


def _parse_value(raw: str) -> object:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def _merge(path: Path, sets: list[str], unsets: list[str]) -> int:
    new: dict[str, object] = {}
    for item in sets:
        if "=" not in item:
            fail(f"--set 需要 键=值 形式：{item}")
        key, raw = item.split("=", 1)
        new[key.strip()] = _parse_value(raw.strip())
    res = resolve([Layer("new", new)])
    bad = [e for e in res.report if e["status"] not in ("applied", "adjusted")]
    if bad:
        emit_json({"ok": False, "rejected": bad})
        return 1
    current: dict[str, object] = {}
    if path.is_file():
        current = load_layer(path).values
    merged = {**current, **new}
    for key in unsets:
        merged.pop(key, None)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(sorted(merged.items())), ensure_ascii=False, indent=2),
                    encoding="utf-8")
    emit_json({"ok": True, "file": str(path), "values": merged})
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--style", action="append", default=[], help="样式层文件，按优先级从高到低重复传入")
    ap.add_argument("--content", help="内容 JSON（用于校验按章节/积木定位的版式键）")
    ap.add_argument("--merge-into", help="个人默认样式文件路径")
    ap.add_argument("--set", action="append", default=[], help="键=值（配合 --merge-into）")
    ap.add_argument("--unset", action="append", default=[], help="要删除的键（配合 --merge-into）")
    args = ap.parse_args(argv)
    try:
        if args.merge_into:
            return _merge(Path(args.merge_into), args.set, args.unset)
        layers = [load_layer(Path(p)) for p in args.style]
        content = load_content(Path(args.content)) if args.content else None
    except ValueError as exc:
        fail(str(exc))
    res = resolve(layers, content)
    emit_json({"style": res.style, "report": res.report})
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 5: 运行测试**

Run: `uv run --no-sync pytest platform-skills/tests/health_plan_report -q`
Expected: 全部 PASS。

break → red → restore → green：
- `resolve` 里去掉 `final_entry[key]["status"] = "overridden"` 这行 → `test_higher_layer_wins_and_lower_is_overridden` 红；还原。
- `_fix_primary` 的阈值 `4.5` 改 `1.0` → `test_low_contrast_primary_is_darkened` 红；还原。
- `_merge` 里 `if bad:` 改 `if False:` → `test_cli_resolve_and_merge` 红（文件被改写）；还原。

- [ ] **Step 6: 同步 spec 裁定 R1**

把 spec §5.1 表中「积木版式」一行改为：

```markdown
| 积木版式 | `blocks.<kind>.variant`（按类型全局）/ `sections.<章节id>.variant` / `sections.<章节id>.<积木id或1起序号>.variant`（精确定位） | 见 §4.3 各积木可选版式；优先级：积木精确 > 章节 > 类型 > 默认 |
```

- [ ] **Step 7: lint 与提交**

```bash
uv run ruff check platform-skills && uv run ruff format --check platform-skills
git add platform-skills/health-plan-report/scripts platform-skills/tests/health_plan_report docs/superpowers/specs/2026-09-28-health-plan-report-skill-design.md
git commit -m "feat(health-plan-report): style layer resolution with plain-language values and parameter report"
```

（`ruff format --check` 若对长字典行报格式问题，运行 `uv run ruff format platform-skills` 后再提交；不要手工对齐。）

---
### Task 3: 主题 token、文字测量、版式原语、积木 → 原语映射、章节图标

**Files:**
- Create: `platform-skills/health-plan-report/scripts/hpr/theme.py`
- Create: `platform-skills/health-plan-report/scripts/hpr/measure.py`
- Create: `platform-skills/health-plan-report/scripts/hpr/prims.py`
- Create: `platform-skills/health-plan-report/scripts/hpr/blocks.py`
- Create: `platform-skills/health-plan-report/scripts/hpr/icons.py`
- Create: `platform-skills/tests/health_plan_report/test_theme_measure_blocks.py`

**Interfaces:**
- Consumes: `hpr.style.resolve / variant_for / mix / contrast`；`hpr.catalog.VARIANTS`
- Produces:
  - `hpr.theme.Theme`（frozen dataclass，字段见代码）；`build_theme(style: dict, fmt: Literal["pptx","pdf"]) -> Theme`；`on_color(bg: str) -> str`（白或墨色中对比更高者）
  - `hpr.measure.Measurer(font_path: str | None = None)`：`width(text, size, bold=False) -> float`、`wrap(text, max_width, size, bold=False) -> list[Line]`、`lines(text, max_width, size, bold=False) -> int`、`split_text(text, max_width, size, n_lines) -> tuple[str, str]`；常量 `SAFETY=1.15`、`LINE=1.35`；`Line(text: str, ends_para: bool)`
  - `hpr.prims`：`Tag`、`RangeBar`、`Card`、`CardGrid`、`Table`、`KeyValue`、`Bullets`、`Paragraph`、`Callout`、`Chart`、`Step`、`Timeline`、`Column`、`Columns`、`Media`、`Image`、`TimeBars`、`SubHeading`；类型别名 `Prim`
  - `hpr.blocks.RenderError(path: str, message: str)`（Exception）；`block_to_prims(block: dict, variant: str, path: str) -> list[Prim]`；`section_prims(content: dict, style: dict) -> list[tuple[dict, list[tuple[Prim, str]]]]`（每章节：章节 dict + [(原语, 来源路径)]）；`fmt(v) -> str`
  - `hpr.icons.ICONS: dict[str, list[list[tuple[float, float]]]]`（24×24 网格折线）；`icon_for_section(section: dict) -> str`

- [ ] **Step 1: 写失败测试 `test_theme_measure_blocks.py`**

```python
import pytest
from hpr.blocks import RenderError, block_to_prims, fmt, section_prims
from hpr.icons import ICONS, icon_for_section
from hpr.measure import SAFETY, Measurer
from hpr.prims import (
    CardGrid,
    Chart,
    Columns,
    KeyValue,
    Paragraph,
    SubHeading,
    Table,
    TimeBars,
    Timeline,
)
from hpr.style import Layer, contrast, resolve
from hpr.theme import build_theme, on_color
from hpr.catalog import VARIANTS


@pytest.fixture
def m():
    return Measurer(font_path="/nonexistent")  # 本地与 CI 用近似测量，确定性


def test_theme_scales_and_density():
    std = build_theme(resolve([]).style, "pptx")
    big = build_theme(resolve([Layer("x", {"type.scale": "large"})]).style, "pptx")
    small = build_theme(resolve([Layer("x", {"type.scale": "compact"})]).style, "pptx")
    assert (std.title, std.heading, std.body, std.caption) == (24, 16, 14, 11)
    assert big.body == std.body + 2
    assert small.body == 13 and small.body >= 12
    pdf = build_theme(resolve([]).style, "pdf")
    assert (pdf.title, pdf.heading, pdf.body, pdf.caption) == (20, 13, 10.5, 8.5)
    airy = build_theme(resolve([Layer("x", {"layout.density": "airy"})]).style, "pptx")
    assert airy.gap_l > std.gap_l


def test_theme_colors_readable():
    t = build_theme(resolve([Layer("x", {"color.background": "浅灰"})]).style, "pptx")
    assert contrast(t.ink, t.background) >= 7
    assert contrast(t.primary, t.background) >= 4.5
    assert contrast(on_color(t.primary), t.primary) >= 4.5


def test_wrap_preserves_text_and_respects_width(m):
    text = "近 30 天空腹血糖有 9 天高于 6.1，建议先稳住餐后血糖，再谈减重速度。Walk 30 minutes daily."
    lines = m.wrap(text, 120, 14)
    assert "".join(ln.text for ln in lines) == text
    assert all(m.width(ln.text, 14) <= 120 / SAFETY + 14 for ln in lines)


def test_wrap_keeps_hard_newlines(m):
    lines = m.wrap("第一行\n第二行", 500, 14)
    assert [ln.text for ln in lines] == ["第一行", "第二行"]
    assert [ln.ends_para for ln in lines] == [True, True]


def test_unbreakable_token_is_split_by_char(m):
    token = "A" * 80
    lines = m.wrap(token, 100, 14)
    assert len(lines) > 1
    assert "".join(ln.text for ln in lines) == token


def test_split_text_round_trip(m):
    text = "甲" * 30 + "\n" + "乙" * 30
    head, tail = m.split_text(text, 100, 14, 3)
    assert m.lines(head, 100, 14) == 3
    assert (head + "\n" + tail).replace("\n", "") == text.replace("\n", "")


def test_real_font_measurement_when_available():
    import os

    path = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
    if not os.path.exists(path):
        pytest.skip("CJK font only in sandbox image")
    real = Measurer(font_path=path)
    assert real.real
    assert 13 < real.width("中", 14) < 15


def test_fmt_numbers():
    assert fmt(6.4) == "6.4"
    assert fmt(26.0) == "26.0"
    assert fmt(398) == "398"
    assert fmt("约 400") == "约 400"


def test_every_kind_and_variant_maps(sample):
    blocks = {b["kind"]: b for s in sample["sections"] for b in s["blocks"]}
    for kind, variants in VARIANTS.items():
        for v in variants:
            prims = block_to_prims(blocks[kind], v, f"x.{kind}")
            assert prims, (kind, v)


def test_profile_cards_have_tags_and_bars(sample):
    prof = next(b for s in sample["sections"] for b in s["blocks"] if b["kind"] == "profile")
    [grid] = block_to_prims(prof, "cards", "p")
    assert isinstance(grid, CardGrid)
    assert grid.cols == 4
    first = grid.cards[0]
    assert first.tag.text == "高于参考范围"
    assert first.tag.tone == "out"
    assert first.bar is not None and first.bar.value == 6.4


def test_profile_without_range_has_tag_no_bar():
    blk = {"kind": "profile", "items": [{"name": "腰围", "value": "92 cm", "position": "above"}]}
    [grid] = block_to_prims(blk, "cards", "p")
    card = grid.cards[0]
    assert card.bar is None
    assert card.tag.text == "高于参考范围"


def test_profile_table_variant(sample):
    prof = next(b for s in sample["sections"] for b in s["blocks"] if b["kind"] == "profile")
    [tbl] = block_to_prims(prof, "table", "p")
    assert isinstance(tbl, Table)
    assert tbl.columns[0] == "指标"
    assert tbl.rows[0][0] == "空腹血糖"


def test_trend_line_and_bar(sample):
    trend = next(b for s in sample["sections"] for b in s["blocks"] if b["kind"] == "trend")
    line = block_to_prims(trend, "line", "t")
    assert isinstance(line[0], SubHeading)
    chart = line[1]
    assert isinstance(chart, Chart) and chart.kind == "line"
    assert (chart.low, chart.high) == (4.4, 6.1)
    bar = block_to_prims(trend, "bar", "t")
    assert bar[1].low is None and bar[1].high is None
    assert any(isinstance(p, Paragraph) and "目标区间" in p.text for p in bar)


def test_donut_requires_numeric_percent():
    blk = {"kind": "nutrition", "macros": [{"name": "碳水", "percent": "约四成"}]}
    with pytest.raises(RenderError) as exc:
        block_to_prims(blk, "donut", "sections[3].blocks[0]")
    assert exc.value.path == "sections[3].blocks[0]"
    assert block_to_prims(blk, "table", "x")  # 表格版式可以


def test_diet_rules_columns_labels(sample):
    rules = next(b for s in sample["sections"] for b in s["blocks"] if b["kind"] == "diet_rules")
    prims = block_to_prims(rules, "columns", "d")
    cols = next(p for p in prims if isinstance(p, Columns))
    assert [c.title for c in cols.columns] == ["推荐", "限制", "避免"]
    assert any(isinstance(p, Table) for p in prims)


def test_sleep_timebar_and_phases_timeline(sample):
    sleep = next(b for s in sample["sections"] for b in s["blocks"] if b["kind"] == "sleep")
    assert isinstance(block_to_prims(sleep, "timebar", "s")[0], TimeBars)
    phases = next(b for s in sample["sections"] for b in s["blocks"] if b["kind"] == "phases")
    assert isinstance(block_to_prims(phases, "timeline", "p")[0], Timeline)


def test_kv_columns(sample):
    kv = next(b for s in sample["sections"] for b in s["blocks"] if b["kind"] == "kv")
    assert block_to_prims(kv, "two-column", "k")[0].cols == 2
    assert block_to_prims(kv, "one-column", "k")[0].cols == 1
    assert isinstance(block_to_prims(kv, "one-column", "k")[0], KeyValue)


def test_section_prims_follow_content_order_and_style(sample):
    style = resolve([Layer("x", {"blocks.meal_plan.variant": "table"})], sample).style
    out = section_prims(sample, style)
    assert [sec["id"] for sec, _ in out] == [s["id"] for s in sample["sections"]]
    diet_items = next(items for sec, items in out if sec["id"] == "diet")
    assert any(isinstance(p, Table) and "食物与用量" in p.columns for p, _ in diet_items)
    assert all(path.startswith("sections[") for _, items in out for _, path in items)


def test_icons_are_in_grid_and_every_section_has_one(sample):
    for name, polylines in ICONS.items():
        for pl in polylines:
            assert len(pl) >= 2, name
            assert all(0 <= x <= 24 and 0 <= y <= 24 for x, y in pl), name
    assert all(icon_for_section(s) in ICONS for s in sample["sections"])
```

- [ ] **Step 2: 运行，确认失败**

Run: `uv run --no-sync pytest platform-skills/tests/health_plan_report/test_theme_measure_blocks.py -q`
Expected: `ModuleNotFoundError: No module named 'hpr.theme'`

- [ ] **Step 3: 写 `hpr/theme.py`**

```python
"""Design tokens for visual direction A (「临床专业」), derived from the resolved style."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from hpr.style import INK, contrast, mix

WITHIN = "#2E7D5B"
OUT = "#B86A0C"
ALERT = "#B42318"
WHITE = "#FFFFFF"

_SIZES = {
    "pptx": {"standard": (24, 16, 14, 11), "large": (26, 18, 16, 13), "compact": (23, 15, 13, 10)},
    "pdf": {"standard": (20, 13, 10.5, 8.5), "large": (21.5, 14.5, 12, 10), "compact": (19.5, 12.5, 10, 8)},
}
_GAPS = {"airy": (6, 12, 18, 28), "standard": (4, 8, 12, 20), "compact": (3, 6, 9, 14)}


@dataclass(frozen=True)
class Theme:
    fmt: str
    primary: str
    accent: str
    background: str
    ink: str
    muted: str
    line: str
    pale: str
    primary_soft: str
    within: str
    out: str
    alert: str
    pale_out: str
    pale_alert: str
    title: float
    heading: float
    body: float
    caption: float
    gap_xs: float
    gap_s: float
    gap_m: float
    gap_l: float
    font_cn: str = "微软雅黑"
    font_latin: str = "Arial"
    font_pdf: str = "Noto Sans CJK SC"

    @property
    def small(self) -> float:
        return max(self.caption, self.body - 2)


def on_color(bg: str) -> str:
    return WHITE if contrast(WHITE, bg) >= contrast(INK, bg) else INK


def build_theme(style: dict[str, Any], fmt: Literal["pptx", "pdf"]) -> Theme:
    primary = style["color.primary"]
    bg = style["color.background"]
    title, heading, body, caption = _SIZES[fmt][style["type.scale"]]
    xs, s, m, lg = _GAPS[style["layout.density"]]
    return Theme(
        fmt=fmt,
        primary=primary,
        accent=style["color.accent"],
        background=bg,
        ink=INK,
        muted=mix(INK, bg, 0.40),
        line=mix(primary, bg, 0.85),
        pale=mix(primary, bg, 0.94),
        primary_soft=mix(primary, WHITE, 0.55),
        within=WITHIN,
        out=OUT,
        alert=ALERT,
        pale_out=mix(OUT, WHITE, 0.90),
        pale_alert=mix(ALERT, WHITE, 0.92),
        title=title,
        heading=heading,
        body=body,
        caption=caption,
        gap_xs=xs,
        gap_s=s,
        gap_m=m,
        gap_l=lg,
    )
```

（`test_theme_colors_readable` 依赖 `muted` 不参与断言；`ink` 恒为 `#1D2B30`，背景由 T2 保证亮度 ≥ 0.80，所以 ≥ 7:1 成立。）

- [ ] **Step 4: 写 `hpr/measure.py`**

```python
"""Text measurement and line wrapping in points (Pillow + the sandbox CJK font, or an
approximation when the font file is absent — local tests and CI)."""

from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

DEFAULT_FONT = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
FONT_ENV = "HPR_FONT_REGULAR"
TTC_INDEX_SC = 2
SAFETY = 1.15
LINE = 1.35
BOLD_FACTOR = 1.04
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9.,:%/+\-~–_]*|\s+|.", re.S)
_CLOSING = frozenset("，。；：！？、）》】」』,.;:!?%)]}")


@dataclass(frozen=True)
class Line:
    text: str
    ends_para: bool


def _wide(ch: str) -> bool:
    return unicodedata.east_asian_width(ch) in ("W", "F")


@lru_cache(maxsize=4)
def _font(path: str):  # type: ignore[no-untyped-def]
    from PIL import ImageFont

    return ImageFont.truetype(path, size=100, index=TTC_INDEX_SC)


class Measurer:
    def __init__(self, font_path: str | None = None) -> None:
        path = font_path or os.environ.get(FONT_ENV) or DEFAULT_FONT
        self._path = path if Path(path).is_file() else None

    @property
    def real(self) -> bool:
        return self._path is not None

    def width(self, text: str, size: float, bold: bool = False) -> float:
        if self._path:
            w = _font(self._path).getlength(text) * size / 100
        else:
            w = sum(size if _wide(ch) else size * 0.55 for ch in text)
        return w * (BOLD_FACTOR if bold else 1.0)

    def wrap(self, text: str, max_width: float, size: float, bold: bool = False) -> list[Line]:
        limit = max_width / SAFETY
        out: list[Line] = []
        for para in text.split("\n"):
            lines: list[str] = []
            cur = ""
            for tok in _TOKEN.findall(para):
                if self.width(cur + tok, size, bold) <= limit:
                    cur += tok
                    continue
                if cur and tok in _CLOSING:
                    cur += tok
                    continue
                if cur:
                    lines.append(cur)
                    cur = ""
                if self.width(tok, size, bold) <= limit:
                    cur = tok
                    continue
                for ch in tok:
                    if cur and self.width(cur + ch, size, bold) > limit:
                        lines.append(cur)
                        cur = ""
                    cur += ch
            lines.append(cur)
            out.extend(Line(ln, ends_para=(i == len(lines) - 1)) for i, ln in enumerate(lines))
        return out

    def lines(self, text: str, max_width: float, size: float, bold: bool = False) -> int:
        return len(self.wrap(text, max_width, size, bold))

    def split_text(self, text: str, max_width: float, size: float, n_lines: int) -> tuple[str, str]:
        wrapped = self.wrap(text, max_width, size)
        head, tail = wrapped[:n_lines], wrapped[n_lines:]

        def join(lines: list[Line]) -> str:
            buf = ""
            for i, ln in enumerate(lines):
                buf += ln.text
                if ln.ends_para and i != len(lines) - 1:
                    buf += "\n"
            return buf

        return join(head), join(tail)
```

- [ ] **Step 5: 写 `hpr/prims.py`**

```python
"""Layout primitives: the small vocabulary both writers (PPT, PDF) know how to draw."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Union


@dataclass(frozen=True)
class Tag:
    text: str
    tone: str  # within | out | alert | info | neutral


@dataclass(frozen=True)
class RangeBar:
    low: float
    high: float
    value: float


@dataclass(frozen=True)
class Card:
    title: str = ""
    value: str = ""
    unit: str = ""
    lines: tuple[str, ...] = ()
    tag: Tag | None = None
    bar: RangeBar | None = None
    badge: str = ""


@dataclass(frozen=True)
class CardGrid:
    cards: tuple[Card, ...]
    cols: int


@dataclass(frozen=True)
class Table:
    columns: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    highlight_col: int | None = None


@dataclass(frozen=True)
class KeyValue:
    pairs: tuple[tuple[str, str], ...]
    cols: int = 2


@dataclass(frozen=True)
class Bullets:
    items: tuple[str, ...]
    style: str = "dots"  # dots | numbers | checks


@dataclass(frozen=True)
class Paragraph:
    text: str
    boxed: bool = False


@dataclass(frozen=True)
class Callout:
    text: str
    title: str = ""
    tone: str = "info"  # info | warn | alert


@dataclass(frozen=True)
class Chart:
    kind: str  # line | bar | donut
    categories: tuple[str, ...]
    values: tuple[float, ...]
    unit: str = ""
    low: float | None = None
    high: float | None = None
    legend: tuple[str, ...] = ()  # donut: one text line per slice (drawn as text, not chart legend)
    center: str = ""  # donut: centre label


@dataclass(frozen=True)
class Step:
    label: str
    lines: tuple[str, ...] = ()


@dataclass(frozen=True)
class Timeline:
    steps: tuple[Step, ...]


@dataclass(frozen=True)
class Column:
    title: str
    items: tuple[str, ...]
    tone: str = "neutral"


@dataclass(frozen=True)
class Columns:
    columns: tuple[Column, ...]


@dataclass(frozen=True)
class Media:
    name: str
    description: str
    url: str
    media_path: str | None = None


@dataclass(frozen=True)
class Image:
    path: str
    caption: str = ""


@dataclass(frozen=True)
class TimeBars:
    rows: tuple[tuple[str, str, str], ...]  # (label, bed HH:MM, wake HH:MM)


@dataclass(frozen=True)
class SubHeading:
    text: str


Prim = Union[
    CardGrid, Table, KeyValue, Bullets, Paragraph, Callout, Chart, Timeline, Columns, Media,
    Image, TimeBars, SubHeading,
]
```

- [ ] **Step 6: 写 `hpr/blocks.py`**

```python
"""Map content blocks to layout primitives. Pure presentation: every text is the caller's,
the only words added here are fixed chrome labels (column headers, status tags)."""

from __future__ import annotations

from typing import Any

from hpr.catalog import VARIANTS
from hpr.prims import (
    Bullets,
    Callout,
    Card,
    CardGrid,
    Chart,
    Column,
    Columns,
    Image,
    KeyValue,
    Media,
    Paragraph,
    Prim,
    RangeBar,
    Step,
    SubHeading,
    Table,
    Tag,
    TimeBars,
    Timeline,
)
from hpr.style import variant_for

DASH = "—"
_POSITION_TAG = {
    "within": Tag("在参考范围内", "within"),
    "above": Tag("高于参考范围", "out"),
    "below": Tag("低于参考范围", "out"),
}
_LEVEL_TAG = {"focus": Tag("重点关注", "out"), "watch": Tag("留意", "info")}


class RenderError(Exception):
    def __init__(self, path: str, message: str) -> None:
        super().__init__(f"{path}: {message}")
        self.path = path
        self.message = message


def fmt(v: Any) -> str:
    """Numbers keep the caller's precision (26.0 stays "26.0"); strings pass through."""
    return str(v)


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _join(*parts: str, sep: str = " ") -> str:
    return sep.join(p for p in parts if p)


def _table(columns: list[str], rows: list[list[str]], highlight: str | None = None) -> Table:
    """Drop columns that are empty in every row (keeps caller text; only removes our dashes)."""
    keep = [i for i in range(len(columns)) if i == 0 or any(r[i] not in ("", DASH) for r in rows)]
    cols = tuple(columns[i] for i in keep)
    body = tuple(tuple(r[i] or DASH for i in keep) for r in rows)
    hl = cols.index(highlight) if highlight in cols else None
    return Table(cols, body, hl)


def _grid(cards: list[Card], max_cols: int) -> CardGrid:
    return CardGrid(tuple(cards), cols=max(1, min(max_cols, len(cards))))


def _ref_text(item: dict) -> str:
    if item.get("ref_text"):
        return item["ref_text"]
    lo, hi = item.get("ref_low"), item.get("ref_high")
    if lo is not None and hi is not None:
        return f"参考 {fmt(lo)}–{fmt(hi)}"
    if lo is not None:
        return f"参考 ≥ {fmt(lo)}"
    if hi is not None:
        return f"参考 ≤ {fmt(hi)}"
    return ""


def _profile(b: dict, v: str) -> list[Prim]:
    items = b["items"]
    if v == "table":
        rows = [
            [
                it["name"],
                _join(fmt(it["value"]), it.get("unit", "")),
                _ref_text(it) or DASH,
                _POSITION_TAG[it["position"]].text if it.get("position") in _POSITION_TAG else DASH,
            ]
            for it in items
        ]
        return [_table(["指标", "当前值", "参考范围", "对照"], rows, highlight="对照")]
    cards = []
    for it in items:
        lo, hi, val = it.get("ref_low"), it.get("ref_high"), it["value"]
        bar = RangeBar(lo, hi, val) if lo is not None and hi is not None and _num(val) else None
        ref = _ref_text(it)
        cards.append(
            Card(
                title=it["name"],
                value=fmt(val),
                unit=it.get("unit", ""),
                lines=(ref,) if ref else (),
                tag=_POSITION_TAG.get(it.get("position", "")),
                bar=bar,
            )
        )
    return [_grid(cards, 4)]


def _issues(b: dict, v: str) -> list[Prim]:
    items = b["items"]
    if v == "list":
        return [Bullets(tuple(_join(i["title"], i.get("evidence", ""), sep="：") for i in items), "numbers")]
    cards = [
        Card(badge=f"{n:02d}", title=i["title"], lines=(i["evidence"],) if i.get("evidence") else (),
             tag=_LEVEL_TAG.get(i.get("level", "")))
        for n, i in enumerate(items, start=1)
    ]
    return [_grid(cards, 3)]


def _trend(b: dict, v: str) -> list[Prim]:
    unit = b.get("unit", "")
    head = SubHeading(b["metric"] + (f"（{unit}）" if unit else ""))
    cats = tuple(p["date"] for p in b["points"])
    vals = tuple(float(p["value"]) for p in b["points"])
    lo, hi = b.get("target_low"), b.get("target_high")
    out: list[Prim] = [head]
    if v == "bar":
        out.append(Chart("bar", cats, vals, unit))
        if lo is not None or hi is not None:
            rng = "–".join(fmt(x) for x in (lo, hi) if x is not None)
            out.append(Paragraph(_join(f"目标区间 {rng}", unit)))
    else:
        out.append(Chart("line", cats, vals, unit, low=lo, high=hi))
    if b.get("ref_text"):
        out.append(Paragraph(b["ref_text"]))
    return out


def _goals(b: dict, v: str) -> list[Prim]:
    items = b["items"]
    if v == "table":
        rows = [
            [i["name"], fmt(i["current"]) if "current" in i else DASH, fmt(i["target"]),
             i.get("unit", "") or DASH, i.get("due", "") or DASH, i.get("note", "") or DASH]
            for i in items
        ]
        return [_table(["目标", "当前", "目标值", "单位", "期限", "说明"], rows)]
    cards = []
    for i in items:
        value = f"{fmt(i['current'])} → {fmt(i['target'])}" if "current" in i else fmt(i["target"])
        lines = tuple(x for x in (i.get("due", ""), i.get("note", "")) if x)
        cards.append(Card(title=i["name"], value=value, unit=i.get("unit", ""), lines=lines))
    return [_grid(cards, 3)]


def _phases(b: dict, v: str) -> list[Prim]:
    items = b["items"]
    if v == "table":
        areas: list[str] = []
        for it in items:
            for f in it["focus"]:
                if f["area"] not in areas:
                    areas.append(f["area"])
        rows = [
            [it["label"], *({f["area"]: f["text"] for f in it["focus"]}.get(a, DASH) for a in areas)]
            for it in items
        ]
        return [_table(["阶段", *areas], rows)]
    if v == "columns":
        return [Columns(tuple(
            Column(it["label"], tuple(f"{f['area']}：{f['text']}" for f in it["focus"])) for it in items
        ))]
    return [Timeline(tuple(
        Step(it["label"], tuple(f"{f['area']}：{f['text']}" for f in it["focus"])) for it in items
    ))]


def _nutrition(b: dict, v: str, path: str) -> list[Prim]:
    out: list[Prim] = []
    macros = b.get("macros") or []
    energy = fmt(b["energy_kcal"]) + " kcal" if "energy_kcal" in b else ""
    if v == "donut" and macros:
        bad = [m for m in macros if not _num(m.get("percent"))]
        if bad:
            raise RenderError(path, "环形图需要每个营养素的 percent 都是数字；请改用表格版式（table）")
        legend = tuple(
            _join(m["name"], f"{fmt(m['grams'])} g" if "grams" in m else "", f"{fmt(m['percent'])}%", sep="　")
            for m in macros
        )
        out.append(Chart("donut", tuple(m["name"] for m in macros),
                         tuple(float(m["percent"]) for m in macros), legend=legend, center=energy))
    else:
        if energy:
            out.append(_grid([Card(title="每日总热量", value=energy)], 1))
        if macros:
            out.append(_table(
                ["营养素", "克数", "占比"],
                [[m["name"], fmt(m["grams"]) if "grams" in m else DASH,
                  f"{fmt(m['percent'])}%" if "percent" in m else DASH] for m in macros],
            ))
    if b.get("meals"):
        out.append(_table(
            ["餐次", "占比", "说明"],
            [[m["name"], f"{fmt(m['percent'])}%" if "percent" in m else DASH, m.get("note", "") or DASH]
             for m in b["meals"]],
        ))
    return out


def _foods(meal: dict) -> tuple[str, ...]:
    return tuple(_join(f["name"], f.get("amount", "")) for f in meal["foods"])


def _meal_plan(b: dict, v: str) -> list[Prim]:
    out: list[Prim] = []
    for tpl in b["templates"]:
        title = _join(tpl.get("name", ""), f"（{tpl['applies_to']}）" if tpl.get("applies_to") else "", sep="")
        if title:
            out.append(SubHeading(title))
        meals = tpl["meals"]
        if v == "table":
            out.append(_table(
                ["餐次", "时间", "食物与用量", "热量"],
                [[m["name"], m.get("time", "") or DASH, "、".join(_foods(m)),
                  f"{fmt(m['kcal'])} kcal" if "kcal" in m else DASH] for m in meals],
            ))
        elif v == "cards":
            out.append(_grid([
                Card(title=_join(m["name"], m.get("time", "")),
                     value=f"{fmt(m['kcal'])}" if "kcal" in m else "",
                     unit="kcal" if "kcal" in m else "", lines=_foods(m))
                for m in meals
            ], 3))
        else:
            out.append(Timeline(tuple(
                Step(_join(m["name"], m.get("time", "")),
                     _foods(m) + ((f"{fmt(m['kcal'])} kcal",) if "kcal" in m else ()))
                for m in meals
            )))
    return out


def _diet_rules(b: dict, v: str) -> list[Prim]:
    groups = [("推荐", b.get("recommend"), "within"), ("限制", b.get("limit"), "out"),
              ("避免", b.get("avoid"), "alert")]
    groups = [(t, items, tone) for t, items, tone in groups if items]
    out: list[Prim] = []
    if v == "list":
        for t, items, _tone in groups:
            out += [SubHeading(t), Bullets(tuple(items))]
    elif groups:
        out.append(Columns(tuple(Column(t, tuple(items), tone) for t, items, tone in groups)))
    if b.get("swaps"):
        out.append(Table(("替换前", "替换为"), tuple((s["from"], s["to"]) for s in b["swaps"])))
    return out


_FITT = (("frequency", "频率"), ("intensity", "强度"), ("time", "时长"), ("type", "类型"))


def _exercise(b: dict, v: str) -> list[Prim]:
    out: list[Prim] = []
    fitt = b.get("fitt") or {}
    cards = [Card(title=label, lines=(fitt[k],)) for k, label in _FITT if k in fitt]
    if cards:
        out.append(_grid(cards, 4))
    if b.get("schedule"):
        if v == "cards":
            out.append(_grid([Card(title=d["day"], lines=tuple(d["items"])) for d in b["schedule"]], 3))
        else:
            out.append(Table(("日期", "安排"), tuple((d["day"], "、".join(d["items"])) for d in b["schedule"])))
    if b.get("progression"):
        out.append(Paragraph(b["progression"], boxed=True))
    if b.get("cautions"):
        out.append(Callout("\n".join(b["cautions"]), title="运动注意", tone="warn"))
    return out


def _sleep(b: dict, v: str) -> list[Prim]:
    out: list[Prim] = []
    rows = [(label, b[k]["bed"], b[k]["wake"]) for k, label in (("current", "当前"), ("target", "目标")) if k in b]
    if rows:
        if v == "list":
            out.append(KeyValue(tuple((f"{label}作息", f"{bed} – {wake}") for label, bed, wake in rows), cols=2))
        else:
            out.append(TimeBars(tuple(rows)))
    if b.get("tips"):
        out.append(Bullets(tuple(b["tips"])))
    return out


def _stress(b: dict, v: str) -> list[Prim]:
    out: list[Prim] = [Paragraph(b["status"])] if b.get("status") else []
    methods = b.get("methods") or []
    if methods and v == "list":
        out.append(Bullets(tuple(_join(m["name"], _join(m.get("how", ""), m.get("frequency", ""), sep="，"), sep="：")
                                 for m in methods)))
    elif methods:
        out.append(_grid([Card(title=m["name"], lines=tuple(x for x in (m.get("how", ""), m.get("frequency", "")) if x))
                          for m in methods], 3))
    return out


def _habits(b: dict, v: str) -> list[Prim]:
    items = b["items"]
    if v == "table":
        return [_table(["习惯", "现状", "目标", "方法"],
                       [[i["name"], i.get("current", "") or DASH, i.get("target", "") or DASH,
                         i.get("how", "") or DASH] for i in items])]
    cards = []
    for i in items:
        value = " → ".join(x for x in (i.get("current", ""), i.get("target", "")) if x)
        cards.append(Card(title=i["name"], lines=tuple(x for x in (value, i.get("how", "")) if x)))
    return [_grid(cards, 3)]


def _monitoring(b: dict, v: str) -> list[Prim]:
    items = b["items"]
    if v == "cards":
        return [_grid([Card(title=i["item"], lines=tuple(x for x in (i.get("frequency", ""), i.get("timing", "")) if x),
                            tag=Tag(i["alert"], "out") if i.get("alert") else None) for i in items], 3)]
    return [_table(["项目", "频次", "时间点", "提醒阈值"],
                   [[i["item"], i.get("frequency", "") or DASH, i.get("timing", "") or DASH,
                     i.get("alert", "") or DASH] for i in items], highlight="提醒阈值")]


def _shopping(b: dict, v: str) -> list[Prim]:
    if v == "list":
        out: list[Prim] = []
        for g in b["groups"]:
            out += [SubHeading(g["name"]), Bullets(tuple(g["items"]))]
        return out
    return [Columns(tuple(Column(g["name"], tuple(g["items"])) for g in b["groups"]))]


def _follow_up(b: dict, v: str) -> list[Prim]:
    pairs = tuple((i["label"], i["value"]) for i in b["items"])
    if v == "table":
        return [KeyValue(pairs, cols=1)]
    return [_grid([Card(title=k, lines=(val,)) for k, val in pairs], 4)]


def _summary(b: dict, v: str) -> list[Prim]:
    if v == "list":
        return [KeyValue(tuple((i["label"], i["text"]) for i in b["items"]), cols=1)]
    return [_grid([Card(title=i["label"], lines=(i["text"],)) for i in b["items"]], 3)]


def block_to_prims(block: dict, variant: str, path: str) -> list[Prim]:
    kind = block["kind"]
    if variant not in VARIANTS[kind]:
        raise RenderError(path, f"积木 {kind} 不支持版式 {variant}")
    if kind == "summary":
        return _summary(block, variant)
    if kind == "profile":
        return _profile(block, variant)
    if kind == "issues":
        return _issues(block, variant)
    if kind == "trend":
        return _trend(block, variant)
    if kind == "goals":
        return _goals(block, variant)
    if kind == "phases":
        return _phases(block, variant)
    if kind == "nutrition":
        return _nutrition(block, variant, path)
    if kind == "meal_plan":
        return _meal_plan(block, variant)
    if kind == "diet_rules":
        return _diet_rules(block, variant)
    if kind == "exercise":
        return _exercise(block, variant)
    if kind == "sleep":
        return _sleep(block, variant)
    if kind == "stress":
        return _stress(block, variant)
    if kind == "habits":
        return _habits(block, variant)
    if kind == "material":
        return [Media(block["name"], block["description"], block["url"], block.get("media_path"))]
    if kind == "monitoring":
        return _monitoring(block, variant)
    if kind == "referral":
        return [Callout(block["text"], tone="alert")]
    if kind == "shopping":
        return _shopping(block, variant)
    if kind == "follow_up":
        return _follow_up(block, variant)
    if kind == "paragraph":
        return [Paragraph(block["text"], boxed=variant == "boxed")]
    if kind == "bullets":
        return [Bullets(tuple(block["items"]), variant)]
    if kind == "table":
        return [Table(tuple(block["columns"]), tuple(tuple(r) for r in block["rows"]))]
    if kind == "kv":
        return [KeyValue(tuple((i["label"], i["value"]) for i in block["items"]),
                         cols=2 if variant == "two-column" else 1)]
    if kind == "image":
        return [Image(block["path"], block.get("caption", ""))]
    if kind == "callout":
        return [Callout(block["text"], block.get("title", ""),
                        "warn" if block.get("level") == "warn" else "info")]
    raise RenderError(path, f"未实现的积木类型 {kind}")


def section_prims(content: dict, style: dict) -> list[tuple[dict, list[tuple[Prim, str]]]]:
    out: list[tuple[dict, list[tuple[Prim, str]]]] = []
    for si, sec in enumerate(content["sections"]):
        items: list[tuple[Prim, str]] = []
        for bi, blk in enumerate(sec["blocks"], start=1):
            path = f"sections[{si}].blocks[{bi - 1}]"
            variant = variant_for(style, blk["kind"], sec["id"], blk.get("id"), bi)
            items += [(p, path) for p in block_to_prims(blk, variant, path)]
        out.append((sec, items))
    return out
```

说明（边界检查，实现者照做）：本模块新增的文字只有固定标签（「指标 / 当前值 / 参考范围 / 对照 / 在参考范围内 / 高于参考范围 / 低于参考范围 / 重点关注 / 留意 / 每日总热量 / 营养素 / 克数 / 占比 / 餐次 / 时间 / 食物与用量 / 热量 / 推荐 / 限制 / 避免 / 替换前 / 替换为 / 频率 / 强度 / 时长 / 类型 / 日期 / 安排 / 运动注意 / 当前 / 目标 / 作息 / 目标区间 / 习惯 / 现状 / 方法 / 项目 / 频次 / 时间点 / 提醒阈值 / 阶段 / 说明 / 单位 / 期限 / 参考」）。不得在此模块加入任何判断性、建议性文字。

- [ ] **Step 7: 写 `hpr/icons.py`**

```python
"""Section icons as polylines on a 24×24 grid (drawn as PPT freeforms and PDF SVG)."""

from __future__ import annotations

import math

Polyline = list[tuple[float, float]]


def _circle(cx: float, cy: float, r: float, n: int = 16) -> Polyline:
    return [(cx + r * math.cos(2 * math.pi * i / n), cy + r * math.sin(2 * math.pi * i / n)) for i in range(n + 1)]


ICONS: dict[str, list[Polyline]] = {
    "pulse": [[(2, 13), (7, 13), (9, 7), (13, 18), (15, 11), (22, 11)]],
    "chart": [[(4, 4), (4, 20), (21, 20)], [(7, 15), (11, 11), (14, 13), (20, 6)]],
    "bowl": [[(3, 11), (21, 11), (19, 17), (15, 20), (9, 20), (5, 17), (3, 11)], [(9, 7), (10, 4)], [(14, 7), (15, 4)]],
    "run": [[(12, 3), (15, 3), (15, 6), (12, 6), (12, 3)], [(9, 10), (14, 8), (16, 12), (19, 13)],
            [(14, 8), (12, 14), (15, 17), (15, 21)], [(12, 14), (8, 17), (5, 17)]],
    "moon": [[(15, 3), (10, 5), (7, 9), (7, 15), (10, 19), (15, 21), (19, 19), (14, 18), (11, 15), (11, 9), (14, 5), (15, 3)]],
    "leaf": [[(5, 19), (5, 11), (10, 6), (19, 5), (18, 14), (13, 19), (5, 19)], [(5, 19), (14, 10)]],
    "repeat": [[(4, 10), (4, 7), (18, 7)], [(15, 4), (18, 7), (15, 10)], [(20, 14), (20, 17), (6, 17)], [(9, 14), (6, 17), (9, 20)]],
    "clipboard": [[(6, 5), (18, 5), (18, 21), (6, 21), (6, 5)], [(9, 3), (15, 3), (15, 7), (9, 7), (9, 3)],
                  [(9, 12), (15, 12)], [(9, 16), (15, 16)]],
    "alert": [[(12, 3), (22, 20), (2, 20), (12, 3)], [(12, 9), (12, 14)], [(12, 17), (12, 17.6)]],
    "bag": [[(5, 8), (19, 8), (18, 21), (6, 21), (5, 8)], [(9, 8), (9, 6), (12, 3), (15, 6), (15, 8)]],
    "calendar": [[(4, 6), (20, 6), (20, 21), (4, 21), (4, 6)], [(4, 10), (20, 10)], [(8, 3), (8, 7)], [(16, 3), (16, 7)]],
    "target": [_circle(12, 12, 9), _circle(12, 12, 5), [(12, 11.5), (12, 12.5)]],
    "flag": [[(5, 21), (5, 4)], [(5, 4), (17, 4), (14, 8), (17, 12), (5, 12)]],
    "search": [_circle(10, 10, 6), [(14.5, 14.5), (21, 21)]],
    "star": [[(12, 3), (14.6, 9), (21, 9.3), (16, 13.3), (17.8, 20), (12, 16.2), (6.2, 20), (8, 13.3), (3, 9.3), (9.4, 9), (12, 3)]],
    "play": [[(6, 4), (19, 12), (6, 20), (6, 4)]],
    "dot": [_circle(12, 12, 4)],
}

_KIND_ICON = {
    "summary": "star", "profile": "pulse", "issues": "search", "trend": "chart", "goals": "target",
    "phases": "flag", "nutrition": "bowl", "meal_plan": "bowl", "diet_rules": "bowl",
    "exercise": "run", "sleep": "moon", "stress": "leaf", "habits": "repeat", "material": "play",
    "monitoring": "clipboard", "referral": "alert", "shopping": "bag", "follow_up": "calendar",
}


def icon_for_section(section: dict) -> str:
    for blk in section["blocks"]:
        if blk["kind"] in _KIND_ICON:
            return _KIND_ICON[blk["kind"]]
    return "dot"
```

- [ ] **Step 8: 运行测试**

Run: `uv run --no-sync pytest platform-skills/tests/health_plan_report -q`
Expected: 全部 PASS（`test_real_font_measurement_when_available` 本地 skip）。

break → red → restore → green：
- `measure.wrap` 里「按字符拆超长 token」分支改成 `cur = tok`（不拆）→ `test_unbreakable_token_is_split_by_char` 红；还原。
- `_profile` 里 `bar = RangeBar(...)` 的条件去掉 `lo is not None and hi is not None` → `test_profile_without_range_has_tag_no_bar` 报 `TypeError` 或断言红；还原。
- `_nutrition` 删掉 `if bad:` 分支 → `test_donut_requires_numeric_percent` 红；还原。

- [ ] **Step 9: lint 与提交**

```bash
uv run ruff format platform-skills/health-plan-report platform-skills/tests/health_plan_report
uv run ruff check platform-skills && uv run ruff format --check platform-skills
git add platform-skills/health-plan-report/scripts/hpr platform-skills/tests/health_plan_report
git commit -m "feat(health-plan-report): theme tokens, text measurement, primitives and block mapping"
```

---
### Task 4: PPT 版式引擎（测高、拆分、分页、短章节合并）

**Files:**
- Create: `platform-skills/health-plan-report/scripts/hpr/ppt_layout.py`
- Create: `platform-skills/tests/health_plan_report/test_ppt_layout.py`

**Interfaces:**
- Consumes: `hpr.prims.*`、`hpr.measure.Measurer / LINE`、`hpr.theme.Theme`、`hpr.icons.icon_for_section`、`hpr.blocks.section_prims`
- Produces（T5 绘制严格按这些几何函数取尺寸，绘制与测高共用同一套计算）：
  - 常量：`SLIDE_W=960.0`、`SLIDE_H=540.0`、`MARGIN_X=40.0`、`BODY_TOP=80.0`、`BODY_W=880.0`、`FOOTER_BOTTOM=526.0`、`MERGE_FILL=0.85`、`CHART_H=200.0`、`DONUT_H=180.0`、`BAR_H=10.0`、`IMAGE_MAX_H=260.0`、`STRIPE=3.0`
  - `LayoutError(path: str, message: str)`（Exception）
  - `Ctx(theme: Theme, m: Measurer, base_dir: Path, body_bottom: float)`；`make_ctx(content, theme, m, base_dir) -> Ctx`（按页脚行数算 `body_bottom`，裁定 R3）
  - `lh(size) -> float`（= size × 1.35）
  - `Part(kind: str, text: str, size: float, bold: bool, height: float)`；`card_parts(card, inner_w, ctx) -> list[Part]`（kind ∈ `title | value | line | tag | bar`）
  - `card_height(card, w, ctx)`、`grid_rows(grid) -> list[tuple[Card, ...]]`、`grid_col_w(grid, w, ctx)`
  - `table_geometry(tbl, w, ctx) -> TableGeo(col_w: list[float], pad_x, pad_y, header_h, row_h: list[float])`
  - `kv_rows(kv) -> list[tuple[tuple[str,str], ...]]`、`kv_geometry(kv, w, ctx) -> (pair_w, label_w, row_h: list[float])`
  - `timeline_rows(tl) -> list[tuple[Step, ...]]`（每行 ≤5 步）、`step_height(step, step_w, ctx)`
  - `columns_rows(cols) -> list[tuple[Column, ...]]`（≤4 一行，否则每行 3）、`column_height(col, col_w, ctx)`
  - `image_size(img, w, ctx) -> tuple[float, float]`（图片框宽高，不含图注）
  - `measure(prim, w, ctx) -> float`；`split(prim, w, avail, ctx) -> tuple[Prim | None, Prim | None]`
  - `Placed(prim, x, y, w, h, path)`；`Page(title: str, icon: str, section_id: str, continued: bool, placed: list[Placed])`
  - `paginate(sections: list[tuple[dict, list[tuple[Prim, str]]]], ctx) -> list[Page]`

- [ ] **Step 1: 写失败测试 `test_ppt_layout.py`**

```python
from pathlib import Path

import pytest
from hpr.blocks import section_prims
from hpr.measure import Measurer
from hpr.ppt_layout import (
    BODY_TOP,
    LayoutError,
    Page,
    make_ctx,
    measure,
    paginate,
    split,
)
from hpr.prims import Bullets, Card, CardGrid, Chart, Column, Columns, Paragraph, SubHeading, Table
from hpr.style import resolve
from hpr.theme import build_theme


def _ctx(content=None, style=None):
    style = style or resolve([]).style
    content = content or {"brand": {}}
    return make_ctx(content, build_theme(style, "pptx"), Measurer("/nonexistent"), Path("."))


def _one(prims, sid="s", title="章节"):
    sec = {"id": sid, "title": title, "blocks": [{"kind": "paragraph", "text": "x"}]}
    return (sec, [(p, f"sections[0].blocks[{i}]") for i, p in enumerate(prims)])


def _within_body(pages: list[Page], ctx) -> bool:
    return all(BODY_TOP - 0.01 <= pl.y and pl.y + pl.h <= ctx.body_bottom + 0.01 for pg in pages for pl in pg.placed)


def test_minimal_content_one_page():
    ctx = _ctx()
    pages = paginate([_one([Paragraph("只有一句话。")])], ctx)
    assert len(pages) == 1
    assert pages[0].title == "章节" and not pages[0].continued


def test_small_sections_merge_onto_one_page():
    ctx = _ctx()
    a = _one([Paragraph("第一节。")], "a", "第一节")
    b = _one([Paragraph("第二节。")], "b", "第二节")
    pages = paginate([a, b], ctx)
    assert len(pages) == 1
    heads = [pl.prim for pl in pages[0].placed if isinstance(pl.prim, SubHeading)]
    assert heads == [SubHeading("第二节")]


def test_long_table_continues_with_header_repeated():
    ctx = _ctx()
    tbl = Table(("日期", "内容"), tuple((f"第 {i} 天", "记录空腹血糖与体重") for i in range(60)))
    pages = paginate([_one([tbl])], ctx)
    assert len(pages) >= 3
    assert pages[1].continued and pages[1].title == "章节（续）"
    pieces = [pl.prim for pg in pages for pl in pg.placed]
    assert all(isinstance(p, Table) and p.columns == ("日期", "内容") for p in pieces)
    assert sum(len(p.rows) for p in pieces) == 60
    assert _within_body(pages, ctx)


def test_paragraph_split_preserves_text():
    ctx = _ctx()
    text = "健康管理建议。" * 400
    pages = paginate([_one([Paragraph(text)])], ctx)
    assert len(pages) > 1
    joined = "".join(pl.prim.text for pg in pages for pl in pg.placed).replace("\n", "")
    assert joined == text
    assert _within_body(pages, ctx)


def test_atomic_too_tall_raises_with_path():
    ctx = _ctx()
    huge = Columns((Column("清单", tuple(f"条目 {i}" for i in range(200))),))
    with pytest.raises(LayoutError) as exc:
        paginate([_one([huge])], ctx)
    assert exc.value.path == "sections[0].blocks[0]"


def test_subheading_kept_with_next():
    ctx = _ctx()
    filler = Paragraph("填充文字。" * 150)
    prims = [filler, SubHeading("空腹血糖（mmol/L）"), Chart("line", ("a", "b"), (1.0, 2.0))]
    pages = paginate([_one(prims)], ctx)
    for pg in pages:
        last = pg.placed[-1].prim
        assert not isinstance(last, SubHeading)


def test_unbreakable_token_wraps_in_card_and_table():
    ctx = _ctx()
    token = "X" * 80
    grid = CardGrid((Card(title="编号", lines=(token,)),), cols=1)
    tbl = Table(("编号",), ((token,),))
    assert measure(grid, 400, ctx) > measure(CardGrid((Card(title="编号", lines=("短",)),), cols=1), 400, ctx)
    pages = paginate([_one([grid, tbl])], ctx)
    assert _within_body(pages, ctx)


def test_split_returns_none_when_nothing_fits():
    ctx = _ctx()
    head, tail = split(Bullets(("一条",)), 880, 1.0, ctx)
    assert head is None and tail == Bullets(("一条",))


def test_disclaimer_grows_footer_and_too_long_errors():
    short = _ctx({"brand": {"org_name": "示例健康管理中心", "disclaimer": "短声明。"}})
    long = _ctx({"brand": {"org_name": "示例健康管理中心", "disclaimer": "声明内容。" * 60}})
    assert long.body_bottom < short.body_bottom
    with pytest.raises(LayoutError) as exc:
        _ctx({"brand": {"disclaimer": "声明内容。" * 400}})
    assert exc.value.path == "brand.disclaimer"


def test_sample_paginates_within_body(sample):
    for sec in sample["sections"]:  # 样例图片由 T5 测试现场生成；版式引擎测试不依赖它
        sec["blocks"] = [b for b in sec["blocks"] if b["kind"] != "image"]
    style = resolve([], sample).style
    ctx = _ctx(sample, style)
    pages = paginate(section_prims(sample, style), ctx)
    assert pages
    assert _within_body(pages, ctx)
    assert [pg.section_id for pg in pages if not pg.continued][0] == sample["sections"][0]["id"]
```

说明：图片缺失属于内容错误，`image_size` 抛 `LayoutError(积木路径, …)`；`render.py`（T5）在分页前统一检查。

- [ ] **Step 2: 运行，确认失败**

Run: `uv run --no-sync pytest platform-skills/tests/health_plan_report/test_ppt_layout.py -q`
Expected: `ModuleNotFoundError: No module named 'hpr.ppt_layout'`

- [ ] **Step 3: 写 `hpr/ppt_layout.py`**

```python
"""PPT layout engine in points: measure primitives, split the splittable ones, paginate
sections onto 16:9 slides (merge short sections, continue long ones with「（续）」)."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path

from hpr.icons import icon_for_section
from hpr.measure import LINE, Measurer
from hpr.prims import (
    Bullets,
    Callout,
    Card,
    CardGrid,
    Chart,
    Column,
    Columns,
    Image,
    KeyValue,
    Media,
    Paragraph,
    Prim,
    Step,
    SubHeading,
    Table,
    TimeBars,
    Timeline,
)
from hpr.theme import Theme

SLIDE_W, SLIDE_H = 960.0, 540.0
MARGIN_X = 40.0
BODY_TOP = 80.0
BODY_W = SLIDE_W - 2 * MARGIN_X
FOOTER_BOTTOM = 526.0
MERGE_FILL = 0.85
CHART_H = 200.0
DONUT_H = 180.0
BAR_H = 10.0
IMAGE_MAX_H = 260.0
STRIPE = 3.0
MAX_DISCLAIMER_LINES = 4


class LayoutError(Exception):
    def __init__(self, path: str, message: str) -> None:
        super().__init__(f"{path}: {message}")
        self.path = path
        self.message = message


@dataclass(frozen=True)
class Ctx:
    theme: Theme
    m: Measurer
    base_dir: Path
    body_bottom: float


def lh(size: float) -> float:
    return size * LINE


def make_ctx(content: dict, theme: Theme, m: Measurer, base_dir: Path) -> Ctx:
    brand = content.get("brand") or {}
    t = theme
    h = 0.0
    if brand.get("org_name") or brand.get("footer_signature"):
        h += lh(t.caption)
    if brand.get("disclaimer"):
        n = m.lines(brand["disclaimer"], BODY_W, t.caption)
        if n > MAX_DISCLAIMER_LINES:
            raise LayoutError("brand.disclaimer", f"免责声明过长（{n} 行），页脚最多 {MAX_DISCLAIMER_LINES} 行")
        h += n * lh(t.caption)
    body_bottom = FOOTER_BOTTOM - h - t.gap_m - t.gap_s
    return Ctx(theme, m, base_dir, body_bottom)


# ---------- cards ----------

@dataclass(frozen=True)
class Part:
    kind: str  # title | value | line | tag | bar
    text: str
    size: float
    bold: bool
    height: float


def card_parts(card: Card, inner: float, ctx: Ctx) -> list[Part]:
    t, m = ctx.theme, ctx.m
    parts: list[Part] = []
    title = f"{card.badge}  {card.title}".strip() if card.badge else card.title
    if title:
        parts.append(Part("title", title, t.caption, True, m.lines(title, inner, t.caption, True) * lh(t.caption)))
    if card.value or card.unit:
        text = f"{card.value} {card.unit}".strip()
        parts.append(Part("value", text, t.title, True, m.lines(text, inner, t.title, True) * lh(t.title)))
    for ln in card.lines:
        parts.append(Part("line", ln, t.small, False, m.lines(ln, inner, t.small) * lh(t.small)))
    if card.tag:
        parts.append(Part("tag", card.tag.text, t.small, True, m.lines(card.tag.text, inner, t.small, True) * lh(t.small)))
    if card.bar:
        parts.append(Part("bar", "", 0, False, BAR_H))
    return parts


def card_height(card: Card, w: float, ctx: Ctx) -> float:
    t = ctx.theme
    parts = card_parts(card, w - 2 * t.gap_m, ctx)
    return STRIPE + 2 * t.gap_m + sum(p.height for p in parts) + t.gap_xs * max(0, len(parts) - 1)


def grid_rows(grid: CardGrid) -> list[tuple[Card, ...]]:
    return [grid.cards[i:i + grid.cols] for i in range(0, len(grid.cards), grid.cols)]


def grid_col_w(grid: CardGrid, w: float, ctx: Ctx) -> float:
    return (w - (grid.cols - 1) * ctx.theme.gap_m) / grid.cols


def _grid_row_heights(grid: CardGrid, w: float, ctx: Ctx) -> list[float]:
    cw = grid_col_w(grid, w, ctx)
    return [max(card_height(c, cw, ctx) for c in row) for row in grid_rows(grid)]


# ---------- tables ----------

@dataclass(frozen=True)
class TableGeo:
    col_w: list[float]
    pad_x: float
    pad_y: float
    header_h: float
    row_h: list[float]


def table_geometry(tbl: Table, w: float, ctx: Ctx) -> TableGeo:
    t, m = ctx.theme, ctx.m
    n = len(tbl.columns)
    longest = [max([m.width(tbl.columns[i], t.small, True)] + [m.width(r[i], t.small) for r in tbl.rows]) for i in range(n)]
    total = sum(longest) or 1.0
    floor_w = w * 0.12
    raw = [max(floor_w, w * x / total) for x in longest]
    scale = w / sum(raw)
    col_w = [x * scale for x in raw]
    pad_x, pad_y = t.gap_s, t.gap_xs + 2
    def row_height(cells: tuple[str, ...], size: float, bold: bool) -> float:
        return max(m.lines(c, cw - 2 * pad_x, size, bold) for c, cw in zip(cells, col_w, strict=True)) * lh(size) + 2 * pad_y
    header_h = row_height(tbl.columns, t.small, True)
    row_h = [row_height(r, t.small, False) for r in tbl.rows]
    return TableGeo(col_w, pad_x, pad_y, header_h, row_h)


# ---------- key-value ----------

def kv_rows(kv: KeyValue) -> list[tuple[tuple[str, str], ...]]:
    return [kv.pairs[i:i + kv.cols] for i in range(0, len(kv.pairs), kv.cols)]


def kv_geometry(kv: KeyValue, w: float, ctx: Ctx) -> tuple[float, float, list[float]]:
    t, m = ctx.theme, ctx.m
    pair_w = (w - (kv.cols - 1) * t.gap_l) / kv.cols
    label_w = pair_w * 0.34
    value_w = pair_w - label_w - t.gap_s
    heights = []
    for row in kv_rows(kv):
        heights.append(max(
            max(m.lines(k, label_w, t.small) * lh(t.small), m.lines(v, value_w, t.body) * lh(t.body))
            for k, v in row
        ) + t.gap_s)
    return pair_w, label_w, heights


# ---------- timeline / columns ----------

def timeline_rows(tl: Timeline) -> list[tuple[Step, ...]]:
    per = 5
    return [tl.steps[i:i + per] for i in range(0, len(tl.steps), per)]


def _step_w(row_len: int, w: float, ctx: Ctx) -> float:
    return (w - (row_len - 1) * ctx.theme.gap_m) / row_len


def step_height(step: Step, step_w: float, ctx: Ctx) -> float:
    t, m = ctx.theme, ctx.m
    inner = step_w - 2 * t.gap_s
    label = m.lines(step.label, step_w, t.body, True) * lh(t.body)
    body = sum(m.lines(ln, inner, t.small) * lh(t.small) for ln in step.lines)
    box = (2 * t.gap_s + body) if step.lines else 0.0
    return 12 + t.gap_s + label + t.gap_xs + box


def _timeline_row_heights(tl: Timeline, w: float, ctx: Ctx) -> list[float]:
    return [max(step_height(s, _step_w(len(row), w, ctx), ctx) for s in row) for row in timeline_rows(tl)]


def columns_rows(cols: Columns) -> list[tuple[Column, ...]]:
    per = len(cols.columns) if len(cols.columns) <= 4 else 3
    return [cols.columns[i:i + per] for i in range(0, len(cols.columns), per)]


def column_height(col: Column, col_w: float, ctx: Ctx) -> float:
    t, m = ctx.theme, ctx.m
    inner = col_w - 2 * t.gap_m
    items = sum(m.lines(it, inner - t.small, t.small) * lh(t.small) for it in col.items)
    return STRIPE + 2 * t.gap_m + lh(t.heading) + t.gap_s + items + t.gap_xs * max(0, len(col.items) - 1)


# ---------- image ----------

def image_size(img: Image, w: float, ctx: Ctx) -> tuple[float, float]:
    from PIL import Image as PILImage

    path = Path(img.path)
    path = path if path.is_absolute() else ctx.base_dir / path
    try:
        with PILImage.open(path) as im:
            iw, ih = im.size
    except (OSError, ValueError) as exc:
        raise LayoutError(img.path, f"找不到图片文件或无法读取：{exc}") from exc
    box_w = min(w, 520.0)
    h = min(IMAGE_MAX_H, box_w * ih / iw)
    return h * iw / ih, h


# ---------- measure ----------

def measure(prim: Prim, w: float, ctx: Ctx) -> float:
    t, m = ctx.theme, ctx.m
    if isinstance(prim, SubHeading):
        return m.lines(prim.text, w, t.heading, True) * lh(t.heading) + t.gap_xs
    if isinstance(prim, Paragraph):
        pad = t.gap_m if prim.boxed else 0.0
        return m.lines(prim.text, w - 2 * pad, t.body) * lh(t.body) + 2 * pad
    if isinstance(prim, Bullets):
        indent = t.body * 1.4
        return sum(m.lines(it, w - indent, t.body) * lh(t.body) for it in prim.items) + t.gap_xs * (len(prim.items) - 1)
    if isinstance(prim, CardGrid):
        rows = _grid_row_heights(prim, w, ctx)
        return sum(rows) + t.gap_m * (len(rows) - 1)
    if isinstance(prim, Table):
        geo = table_geometry(prim, w, ctx)
        return geo.header_h + sum(geo.row_h)
    if isinstance(prim, KeyValue):
        return sum(kv_geometry(prim, w, ctx)[2])
    if isinstance(prim, Callout):
        inner = w - 2 * t.gap_m - 4
        title = (lh(t.body) + t.gap_xs) if prim.title else 0.0
        return 2 * t.gap_m + title + m.lines(prim.text, inner, t.body) * lh(t.body)
    if isinstance(prim, Chart):
        if prim.kind == "donut":
            return max(DONUT_H, len(prim.legend) * (lh(t.body) + t.gap_xs))
        return CHART_H
    if isinstance(prim, Timeline):
        rows = _timeline_row_heights(prim, w, ctx)
        return sum(rows) + t.gap_l * (len(rows) - 1)
    if isinstance(prim, Columns):
        heights = []
        for row in columns_rows(prim):
            cw = (w - (len(row) - 1) * t.gap_m) / len(row)
            heights.append(max(column_height(c, cw, ctx) for c in row))
        return sum(heights) + t.gap_m * (len(heights) - 1)
    if isinstance(prim, Media):
        text_w = w * 0.62 - 2 * t.gap_m
        body = lh(t.heading) + t.gap_xs + m.lines(prim.description, text_w, t.body) * lh(t.body)
        return max(96.0, body + 2 * t.gap_m)
    if isinstance(prim, Image):
        _, h = image_size(prim, w, ctx)
        cap = (t.gap_xs + m.lines(prim.caption, w, t.caption) * lh(t.caption)) if prim.caption else 0.0
        return h + cap
    if isinstance(prim, TimeBars):
        return len(prim.rows) * (lh(t.body) + t.gap_s) + lh(t.caption) + t.gap_s
    raise TypeError(f"unknown primitive {type(prim).__name__}")


# ---------- split ----------

def _fit_count(heights: list[float], gap: float, avail: float) -> int:
    used, n = 0.0, 0
    for i, h in enumerate(heights):
        need = h + (gap if i else 0.0)
        if used + need > avail:
            break
        used += need
        n += 1
    return n


def split(prim: Prim, w: float, avail: float, ctx: Ctx) -> tuple[Prim | None, Prim | None]:
    """Split so that head fits in ``avail``. (None, prim) when nothing fits."""
    t, m = ctx.theme, ctx.m
    if isinstance(prim, Paragraph):
        pad = t.gap_m if prim.boxed else 0.0
        n = int((avail - 2 * pad) // lh(t.body))
        if n < 1:
            return None, prim
        head, tail = m.split_text(prim.text, w - 2 * pad, t.body, n)
        return replace(prim, text=head), (replace(prim, text=tail) if tail else None)
    if isinstance(prim, Bullets):
        indent = t.body * 1.4
        hs = [m.lines(it, w - indent, t.body) * lh(t.body) for it in prim.items]
        k = _fit_count(hs, t.gap_xs, avail)
        return _cut(prim, "items", k)
    if isinstance(prim, CardGrid):
        rows = grid_rows(prim)
        k = _fit_count(_grid_row_heights(prim, w, ctx), t.gap_m, avail)
        if k == 0:
            return None, prim
        head = replace(prim, cards=tuple(c for row in rows[:k] for c in row))
        rest = tuple(c for row in rows[k:] for c in row)
        return head, (replace(prim, cards=rest) if rest else None)
    if isinstance(prim, Table):
        geo = table_geometry(prim, w, ctx)
        k = _fit_count(geo.row_h, 0.0, avail - geo.header_h)
        return _cut(prim, "rows", k)
    if isinstance(prim, KeyValue):
        rows = kv_rows(prim)
        k = _fit_count(kv_geometry(prim, w, ctx)[2], 0.0, avail)
        if k == 0:
            return None, prim
        head = replace(prim, pairs=tuple(p for row in rows[:k] for p in row))
        rest = tuple(p for row in rows[k:] for p in row)
        return head, (replace(prim, pairs=rest) if rest else None)
    if isinstance(prim, Timeline):
        rows = timeline_rows(prim)
        k = _fit_count(_timeline_row_heights(prim, w, ctx), t.gap_l, avail)
        if k == 0:
            return None, prim
        head = replace(prim, steps=tuple(s for row in rows[:k] for s in row))
        rest = tuple(s for row in rows[k:] for s in row)
        return head, (replace(prim, steps=rest) if rest else None)
    return (prim, None) if measure(prim, w, ctx) <= avail else (None, prim)


def _cut(prim: Prim, attr: str, k: int) -> tuple[Prim | None, Prim | None]:
    seq = getattr(prim, attr)
    if k == 0:
        return None, prim
    head = replace(prim, **{attr: seq[:k]})
    tail = replace(prim, **{attr: seq[k:]}) if k < len(seq) else None
    return head, tail


def splittable(prim: Prim) -> bool:
    return isinstance(prim, (Paragraph, Bullets, CardGrid, Table, KeyValue, Timeline))


# ---------- paginate ----------

@dataclass
class Placed:
    prim: Prim
    x: float
    y: float
    w: float
    h: float
    path: str


@dataclass
class Page:
    title: str
    icon: str
    section_id: str
    continued: bool
    placed: list[Placed] = field(default_factory=list)


def paginate(sections: list[tuple[dict, list[tuple[Prim, str]]]], ctx: Ctx) -> list[Page]:
    t = ctx.theme
    bottom = ctx.body_bottom
    avail_total = bottom - BODY_TOP
    pages: list[Page] = []
    cur: Page | None = None
    y = BODY_TOP

    def new_page(sec: dict, continued: bool) -> Page:
        pg = Page(sec["title"] + ("（续）" if continued else ""), icon_for_section(sec), sec["id"], continued)
        pages.append(pg)
        return pg

    def min_head(prim: Prim) -> float:
        full = measure(prim, BODY_W, ctx)
        return min(full, 2 * lh(t.body) + 2 * t.gap_m) if splittable(prim) else full

    for si, (sec, items) in enumerate(sections):
        total = sum(measure(p, BODY_W, ctx) for p, _ in items) + t.gap_l * max(0, len(items) - 1)
        head = SubHeading(sec["title"])
        head_h = measure(head, BODY_W, ctx)
        if cur is not None and cur.placed and (y - BODY_TOP) + t.gap_l + head_h + total <= MERGE_FILL * avail_total:
            y += t.gap_l
            cur.placed.append(Placed(head, MARGIN_X, y, BODY_W, head_h, f"sections[{si}]"))
            y += head_h
        else:
            cur = new_page(sec, continued=False)
            y = BODY_TOP
        for idx, (prim, path) in enumerate(items):
            if idx:
                y += t.gap_l
            pending: Prim | None = prim
            while pending is not None:
                h = measure(pending, BODY_W, ctx)
                need = h
                if isinstance(pending, SubHeading) and idx + 1 < len(items):
                    need = h + t.gap_l + min_head(items[idx + 1][0])  # keep heading with what follows
                at_top = y <= BODY_TOP + 0.01
                if y + need <= bottom or (at_top and h <= avail_total):
                    cur.placed.append(Placed(pending, MARGIN_X, y, BODY_W, h, path))
                    y += h
                    pending = None
                    continue
                if splittable(pending):
                    head_part, tail = split(pending, BODY_W, bottom - y, ctx)
                    if head_part is None and at_top:
                        raise LayoutError(path, "单个条目超过一页，请把内容拆小")
                    if head_part is not None:
                        hh = measure(head_part, BODY_W, ctx)
                        cur.placed.append(Placed(head_part, MARGIN_X, y, BODY_W, hh, path))
                    pending = tail
                elif at_top:
                    raise LayoutError(path, "内容超过一页且无法拆分，请把内容拆小")
                if pending is not None:
                    cur = new_page(sec, continued=True)
                    y = BODY_TOP
    return pages
```

- [ ] **Step 4: 运行测试**

Run: `uv run --no-sync pytest platform-skills/tests/health_plan_report -q`
Expected: 全部 PASS。

break → red → restore → green：
- `MERGE_FILL` 改 `0.0` → `test_small_sections_merge_onto_one_page` 红；还原。
- `split` 的 Table 分支把 `avail - geo.header_h` 改为 `avail` → `test_long_table_continues_with_header_repeated` 的 `_within_body` 红；还原。
- 删掉 SubHeading 的 keep-with-next（`need = h`）→ `test_subheading_kept_with_next` 红；还原。

- [ ] **Step 5: lint 与提交**

```bash
uv run ruff format platform-skills/health-plan-report platform-skills/tests/health_plan_report
uv run ruff check platform-skills && uv run ruff format --check platform-skills
git add platform-skills/health-plan-report/scripts/hpr/ppt_layout.py platform-skills/tests/health_plan_report/test_ppt_layout.py
git commit -m "feat(health-plan-report): PPT layout engine (measure, split, paginate, merge)"
```

---
### Task 5: PPT 绘制 + 原生图表 + 质检 + `render.py`（pptx 路径）

**Files:**
- Create: `platform-skills/health-plan-report/scripts/hpr/ppt_draw.py`
- Create: `platform-skills/health-plan-report/scripts/hpr/ppt_charts.py`
- Create: `platform-skills/health-plan-report/scripts/hpr/qa.py`
- Create: `platform-skills/health-plan-report/scripts/render.py`
- Create: `platform-skills/tests/health_plan_report/test_render_pptx.py`

**Interfaces:**
- Consumes: T1–T4 全部；`hpr.ppt_layout` 的几何函数（绘制尺寸必须来自它们，不得另算）
- Produces:
  - 形状命名约定（裁定 R6）：`hpr:body`（内容文字/表格）、`hpr:chrome`（页标题、页脚、封面信息等）、`hpr:deco`（装饰与生成的标记/刻度/序号）、`hpr:chart`、`hpr:image`、`hpr:logo`、`hpr:media`
  - `hpr.ppt_draw.render_pptx(content: dict, style: dict, out_path: Path, base_dir: Path, m: Measurer) -> list[str]`（返回降级警告；内容/版式错误抛 `RenderError` / `LayoutError`）
  - `hpr.ppt_draw.Canvas(slide, theme)`：`rect / text / icon / picture`
  - `hpr.ppt_charts.add_chart(slide, prim: Chart, x, y, w, h, theme) -> GraphicFrame`、`donut_colors(theme) -> list[str]`
  - `hpr.qa`：`norm(s) -> str`、`missing_texts(required, corpus) -> list[str]`、`pptx_corpus(path) -> str`、`pptx_bounds(path) -> list[str]`、`pptx_overflow(path, m) -> list[str]`、`contrast_report(theme) -> dict`、`qa_pptx(path, required, theme, m) -> dict`（`{"status": "passed"|"failed", "missing", "overflow", "out_of_bounds", "contrast", "slides"}`）
  - CLI：`render.py --content C.json [--style L1.json ...] [--format pptx|pdf|both] --out-dir DIR --basename NAME` → 写 `DIR/NAME.pptx`（或 `.pdf`）、`DIR/NAME.qa.json`、`DIR/NAME.params-report.json`；stdout JSON `{"ok", "files", "qa", "not_applied", "warnings", "errors"}`；退出码 0 = 全部质检通过

- [ ] **Step 1: 写失败测试 `test_render_pptx.py`**

```python
import json
import subprocess
import sys
from pathlib import Path

import pytest
from hpr.content import required_texts
from hpr.measure import Measurer
from hpr.ppt_draw import BODY, Canvas, render_pptx
from hpr.qa import missing_texts, pptx_bounds, pptx_corpus, pptx_overflow, qa_pptx
from hpr.style import Layer, resolve
from hpr.theme import build_theme
from PIL import Image as PILImage
from pptx import Presentation
from pptx.oxml.ns import qn

SKILL = Path(__file__).resolve().parents[2] / "health-plan-report"
RENDER = SKILL / "scripts" / "render.py"
M = Measurer("/nonexistent")


@pytest.fixture
def workdir(tmp_path, sample):
    PILImage.new("RGB", (640, 360), "#DDEEEE").save(tmp_path / "trend-note.png")
    (tmp_path / "plan.json").write_text(json.dumps(sample, ensure_ascii=False), encoding="utf-8")
    return tmp_path


def _render(content, tmp, layers=()):
    style = resolve(list(layers), content).style
    out = tmp / "out.pptx"
    warnings = render_pptx(content, style, out, tmp, M)
    return out, style, warnings


def test_sample_renders_and_passes_qa(workdir, sample):
    out, style, warnings = _render(sample, workdir)
    prs = Presentation(str(out))
    assert prs.slide_width == 12192000 and prs.slide_height == 6858000
    assert len(prs.slides) >= 8
    qa = qa_pptx(out, required_texts(sample), build_theme(style, "pptx"), M)
    assert qa["status"] == "passed", qa
    assert warnings == []


def test_every_required_text_is_found_and_detector_bites(workdir, sample):
    out, _, _ = _render(sample, workdir)
    corpus = pptx_corpus(out)
    assert missing_texts(required_texts(sample), corpus) == []
    assert missing_texts(["一段根本不存在的文字"], corpus) == ["一段根本不存在的文字"]


def test_all_shapes_inside_slide(workdir, sample):
    out, _, _ = _render(sample, workdir)
    assert pptx_bounds(out) == []


def test_bounds_detector_bites(tmp_path):
    prs = Presentation()
    prs.slide_width, prs.slide_height = 12192000, 6858000
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    Canvas(slide, build_theme(resolve([]).style, "pptx")).text(900, 500, 200, 30, "越界", 14, "#000000")
    p = tmp_path / "b.pptx"
    prs.save(str(p))
    assert pptx_bounds(p)


def test_overflow_detector_bites(tmp_path):
    prs = Presentation()
    prs.slide_width, prs.slide_height = 12192000, 6858000
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    Canvas(slide, build_theme(resolve([]).style, "pptx")).text(40, 40, 80, 20, "很长的文字" * 20, 14, "#000000")
    p = tmp_path / "o.pptx"
    prs.save(str(p))
    assert pptx_overflow(p, M)


def test_cjk_font_on_every_run(workdir, sample):
    out, _, _ = _render(sample, workdir)
    prs = Presentation(str(out))
    for slide in prs.slides:
        for sh in slide.shapes:
            if sh.has_text_frame:
                for para in sh.text_frame.paragraphs:
                    for run in para.runs:
                        ea = run.font._element.find(qn("a:ea"))
                        assert ea is not None and ea.get("typeface") == "微软雅黑"
                        assert run.font.name == "Arial"


def test_body_shapes_are_named(workdir, sample):
    out, _, _ = _render(sample, workdir)
    names = {sh.name for s in Presentation(str(out)).slides for sh in s.shapes}
    assert BODY in names
    assert names <= {"hpr:body", "hpr:chrome", "hpr:deco", "hpr:chart", "hpr:image", "hpr:logo", "hpr:media"}


@pytest.mark.parametrize("variant", ["band", "split", "minimal"])
@pytest.mark.parametrize("pos", ["top-left", "top-right", "bottom-left", "bottom-right", "center"])
def test_cover_variants_and_logo_positions(workdir, sample, variant, pos):
    PILImage.new("RGB", (300, 120), "#0B4F5C").save(workdir / "logo.png")
    sample["brand"]["logo_path"] = "logo.png"
    layers = [Layer("x", {"cover.variant": variant, "brand.logo_position": pos, "toc": "on"})]
    out, style, warnings = _render(sample, workdir, layers)
    qa = qa_pptx(out, required_texts(sample), build_theme(style, "pptx"), M)
    assert qa["status"] == "passed", qa
    assert warnings == []
    logos = [sh for sh in Presentation(str(out)).slides[0].shapes if sh.name == "hpr:logo"]
    assert len(logos) == 1


def test_missing_logo_is_a_warning_not_a_failure(workdir, sample):
    sample["brand"]["logo_path"] = "nope.png"
    _, _, warnings = _render(sample, workdir)
    assert any("LOGO" in w for w in warnings)


def test_cli_writes_outputs_and_reports(workdir):
    style = workdir / "style.json"
    style.write_text(json.dumps({"color.primary": "深蓝", "brand.org_name": "别家"}), encoding="utf-8")
    res = subprocess.run(
        [sys.executable, str(RENDER), "--content", str(workdir / "plan.json"), "--style", str(style),
         "--out-dir", str(workdir / "out"), "--basename", "李先生_20260928100000"],
        capture_output=True, text=True, check=False,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    summary = json.loads(res.stdout)
    assert summary["ok"] is True
    assert Path(summary["files"]["pptx"]).is_file()
    assert [e["key"] for e in summary["not_applied"]] == ["brand.org_name"]
    assert (workdir / "out" / "李先生_20260928100000.qa.json").is_file()
    assert (workdir / "out" / "李先生_20260928100000.params-report.json").is_file()


def test_existing_output_is_not_overwritten(workdir):
    args = [sys.executable, str(RENDER), "--content", str(workdir / "plan.json"),
            "--out-dir", str(workdir / "out"), "--basename", "a"]
    assert subprocess.run(args, capture_output=True, check=False).returncode == 0
    target = workdir / "out" / "a.pptx"
    before = target.read_bytes()
    again = subprocess.run(args, capture_output=True, text=True, check=False)
    assert again.returncode == 1
    assert "已存在" in again.stdout + again.stderr
    assert target.read_bytes() == before


def test_cli_invalid_content_exit_1(workdir, sample):
    del sample["title"]
    bad = workdir / "bad.json"
    bad.write_text(json.dumps(sample, ensure_ascii=False), encoding="utf-8")
    res = subprocess.run(
        [sys.executable, str(RENDER), "--content", str(bad), "--out-dir", str(workdir / "o"), "--basename", "x"],
        capture_output=True, text=True, check=False,
    )
    assert res.returncode == 1
    assert "title: 缺少必填字段" in json.loads(res.stdout)["errors"]


def test_missing_image_reports_block_path(workdir, sample):
    (workdir / "trend-note.png").unlink()
    (workdir / "plan.json").write_text(json.dumps(sample, ensure_ascii=False), encoding="utf-8")
    res = subprocess.run(
        [sys.executable, str(RENDER), "--content", str(workdir / "plan.json"), "--out-dir", str(workdir / "o"), "--basename", "x"],
        capture_output=True, text=True, check=False,
    )
    assert res.returncode == 1
    assert any(e.startswith("sections[2].blocks[1]") for e in json.loads(res.stdout)["errors"])


def test_long_title_is_a_clear_error(workdir, sample):
    sample["title"] = "非常长的方案名称" * 12
    with pytest.raises(Exception, match="title"):
        _render(sample, workdir)


def test_basename_with_slash_rejected(workdir):
    res = subprocess.run(
        [sys.executable, str(RENDER), "--content", str(workdir / "plan.json"), "--out-dir", str(workdir / "o"), "--basename", "../x"],
        capture_output=True, text=True, check=False,
    )
    assert res.returncode == 1


def test_pdf_not_yet_available_message(workdir):
    # T7 实现 PDF 后删除本测试（T7 Step 1 会替换它）
    res = subprocess.run(
        [sys.executable, str(RENDER), "--content", str(workdir / "plan.json"), "--format", "pdf",
         "--out-dir", str(workdir / "o"), "--basename", "x"],
        capture_output=True, text=True, check=False,
    )
    assert res.returncode == 1
```

- [ ] **Step 2: 运行，确认失败**

Run: `uv run --no-sync pytest platform-skills/tests/health_plan_report/test_render_pptx.py -q`
Expected: `ModuleNotFoundError: No module named 'hpr.ppt_draw'`

- [ ] **Step 3: 写 `hpr/ppt_charts.py`**

```python
"""Native, editable PowerPoint charts for trend and nutrition primitives."""

from __future__ import annotations

from pptx.chart.data import CategoryChartData
from pptx.dml.color import RGBColor
from pptx.enum.chart import XL_CHART_TYPE, XL_LEGEND_POSITION, XL_MARKER_STYLE
from pptx.enum.dml import MSO_LINE_DASH_STYLE
from pptx.oxml.ns import qn
from pptx.oxml.xmlchemy import OxmlElement
from pptx.util import Emu, Pt

from hpr.prims import Chart
from hpr.style import mix
from hpr.theme import Theme


def _emu(pt: float) -> Emu:
    return Emu(int(round(pt * 12700)))


def _rgb(hex_: str) -> RGBColor:
    return RGBColor.from_string(hex_[1:])


def donut_colors(t: Theme) -> list[str]:
    return [t.primary, t.accent, t.primary_soft, mix(t.primary, t.background, 0.75), t.muted, t.within]


def add_chart(slide, prim: Chart, x: float, y: float, w: float, h: float, t: Theme):  # type: ignore[no-untyped-def]
    if prim.kind == "donut":
        return _donut(slide, prim, x, y, min(w, h), t)
    cd = CategoryChartData()
    cd.categories = list(prim.categories)
    cd.add_series(prim.unit or "数值", list(prim.values))
    has_bounds = prim.kind == "line" and (prim.low is not None or prim.high is not None)
    if prim.kind == "line":
        if prim.low is not None:
            cd.add_series("目标下限", [prim.low] * len(prim.values))
        if prim.high is not None:
            cd.add_series("目标上限", [prim.high] * len(prim.values))
    ctype = XL_CHART_TYPE.LINE_MARKERS if prim.kind == "line" else XL_CHART_TYPE.COLUMN_CLUSTERED
    gf = slide.shapes.add_chart(ctype, _emu(x), _emu(y), _emu(w), _emu(h), cd)
    gf.name = "hpr:chart"
    ch = gf.chart
    ch.font.size = Pt(t.caption)
    ch.font.name = t.font_latin
    ch.font.color.rgb = _rgb(t.muted)
    ch.has_legend = has_bounds
    if has_bounds:
        ch.legend.position = XL_LEGEND_POSITION.BOTTOM
        ch.legend.include_in_layout = False
    values = list(prim.values) + [v for v in (prim.low, prim.high) if v is not None]
    lo, hi = min(values), max(values)
    pad = (hi - lo) * 0.15 or 1.0
    va = ch.value_axis
    va.has_major_gridlines = True
    va.major_gridlines.format.line.color.rgb = _rgb(t.line)
    va.format.line.fill.background()
    va.minimum_scale = 0 if prim.kind == "bar" and lo >= 0 else round(lo - pad, 2)
    va.maximum_scale = round(hi + pad, 2)
    va.tick_labels.font.size = Pt(t.caption)
    ca = ch.category_axis
    ca.format.line.color.rgb = _rgb(t.line)
    ca.tick_labels.font.size = Pt(t.caption)
    plot = ch.plots[0]
    first = plot.series[0]
    if prim.kind == "line":
        first.smooth = False
        first.format.line.color.rgb = _rgb(t.primary)
        first.format.line.width = Pt(2)
        first.marker.style = XL_MARKER_STYLE.CIRCLE
        first.marker.size = 5
        first.marker.format.fill.solid()
        first.marker.format.fill.fore_color.rgb = _rgb(t.primary)
        first.marker.format.line.color.rgb = _rgb(t.primary)
        for s in list(plot.series)[1:]:
            s.smooth = False
            s.format.line.color.rgb = _rgb(t.within)
            s.format.line.width = Pt(1.25)
            s.format.line.dash_style = MSO_LINE_DASH_STYLE.DASH
            s.marker.style = XL_MARKER_STYLE.NONE
    else:
        plot.gap_width = 80
        first.format.fill.solid()
        first.format.fill.fore_color.rgb = _rgb(t.primary)
    return gf


def _donut(slide, prim: Chart, x: float, y: float, size: float, t: Theme):  # type: ignore[no-untyped-def]
    cd = CategoryChartData()
    cd.categories = list(prim.categories)
    cd.add_series("占比", list(prim.values))
    gf = slide.shapes.add_chart(XL_CHART_TYPE.DOUGHNUT, _emu(x), _emu(y), _emu(size), _emu(size), cd)
    gf.name = "hpr:chart"
    ch = gf.chart
    ch.has_legend = False
    plot = ch.plots[0]
    plot.has_data_labels = False
    colors = donut_colors(t)
    for i, point in enumerate(plot.series[0].points):
        point.format.fill.solid()
        point.format.fill.fore_color.rgb = _rgb(colors[i % len(colors)])
        point.format.line.color.rgb = _rgb(t.background)
    hole = plot._element.find(qn("c:holeSize"))
    if hole is None:
        hole = OxmlElement("c:holeSize")
        plot._element.append(hole)
    hole.set("val", "62")
    return gf
```

- [ ] **Step 4: 写 `hpr/ppt_draw.py`**

```python
"""Draw a paginated plan onto a 16:9 deck (python-pptx native objects, visual direction A)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, MSO_AUTO_SIZE, PP_ALIGN
from pptx.oxml.ns import qn
from pptx.oxml.xmlchemy import OxmlElement
from pptx.util import Emu, Pt

from hpr.blocks import section_prims
from hpr.icons import ICONS
from hpr.measure import LINE, Measurer
from hpr.ppt_charts import add_chart, donut_colors
from hpr.ppt_layout import (
    BODY_TOP,
    BODY_W,
    FOOTER_BOTTOM,
    MARGIN_X,
    SLIDE_H,
    SLIDE_W,
    STRIPE,
    Ctx,
    LayoutError,
    Page,
    Placed,
    card_height,
    card_parts,
    column_height,
    columns_rows,
    grid_col_w,
    grid_rows,
    image_size,
    kv_geometry,
    kv_rows,
    lh,
    make_ctx,
    paginate,
    step_height,
    table_geometry,
    timeline_rows,
)
from hpr.prims import (
    Bullets,
    Callout,
    CardGrid,
    Chart,
    Columns,
    Image,
    KeyValue,
    Media,
    Paragraph,
    SubHeading,
    Table,
    TimeBars,
    Timeline,
)
from hpr.style import mix
from hpr.theme import Theme, build_theme, on_color

BODY, CHROME, DECO = "hpr:body", "hpr:chrome", "hpr:deco"
NO_GRID_STYLE = "{2D5ABB26-0587-4C30-8999-92F81FD0307C}"
_ALIGN = {"left": PP_ALIGN.LEFT, "center": PP_ALIGN.CENTER, "right": PP_ALIGN.RIGHT}
_TONE = {"within": "within", "out": "out", "alert": "alert", "info": "primary", "neutral": "muted"}


def emu(pt: float) -> Emu:
    return Emu(int(round(pt * 12700)))


def rgb(hex_: str) -> RGBColor:
    return RGBColor.from_string(hex_[1:])


def _set_font(run: Any, t: Theme, size: float, color: str, bold: bool) -> None:
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.color.rgb = rgb(color)
    run.font.name = t.font_latin
    rpr = run.font._element
    ea = rpr.find(qn("a:ea"))
    if ea is None:
        ea = OxmlElement("a:ea")
        rpr.insert_element_before(ea, "a:cs", "a:sym", "a:hlinkClick", "a:hlinkMouseOver", "a:rtl", "a:extLst")
    ea.set("typeface", t.font_cn)


class Canvas:
    def __init__(self, slide: Any, theme: Theme) -> None:
        self.slide = slide
        self.t = theme

    def rect(self, x: float, y: float, w: float, h: float, fill: str | None, *, line: str | None = None,
             rounded: bool = False, name: str = DECO) -> Any:
        kind = MSO_SHAPE.ROUNDED_RECTANGLE if rounded else MSO_SHAPE.RECTANGLE
        shp = self.slide.shapes.add_shape(kind, emu(x), emu(y), emu(w), emu(h))
        shp.name = name
        if rounded:
            shp.adjustments[0] = min(0.5, 4.0 / max(1.0, min(w, h)))
        if fill:
            shp.fill.solid()
            shp.fill.fore_color.rgb = rgb(fill)
        else:
            shp.fill.background()
        if line:
            shp.line.color.rgb = rgb(line)
            shp.line.width = Pt(0.75)
        else:
            shp.line.fill.background()
        shp.shadow.inherit = False
        return shp

    def oval(self, x: float, y: float, w: float, h: float, fill: str | None, line: str | None = None) -> Any:
        shp = self.slide.shapes.add_shape(MSO_SHAPE.OVAL, emu(x), emu(y), emu(w), emu(h))
        shp.name = DECO
        if fill:
            shp.fill.solid()
            shp.fill.fore_color.rgb = rgb(fill)
        else:
            shp.fill.background()
        if line:
            shp.line.color.rgb = rgb(line)
            shp.line.width = Pt(0.75)
        else:
            shp.line.fill.background()
        shp.shadow.inherit = False
        return shp

    def text(self, x: float, y: float, w: float, h: float, text: str, size: float, color: str, *,
             bold: bool = False, align: str = "left", name: str = BODY, link: str | None = None) -> Any:
        tb = self.slide.shapes.add_textbox(emu(x), emu(y), emu(w), emu(h))
        tb.name = name
        tf = tb.text_frame
        tf.word_wrap = True
        tf.auto_size = MSO_AUTO_SIZE.NONE
        tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
        tf.vertical_anchor = MSO_ANCHOR.TOP
        for i, para_text in enumerate(text.split("\n")):
            p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
            p.alignment = _ALIGN[align]
            p.line_spacing = Pt(size * LINE)
            r = p.add_run()
            r.text = para_text
            _set_font(r, self.t, size, color, bold)
            if link:
                r.hyperlink.address = link
        return tb

    def polyline(self, pts: list[tuple[float, float]], color: str, width: float) -> Any:
        (x0, y0), rest = pts[0], pts[1:]
        builder = self.slide.shapes.build_freeform(emu(x0), emu(y0), scale=1.0)
        builder.add_line_segments([(emu(x), emu(y)) for x, y in rest], close=False)
        shp = builder.convert_to_shape()
        shp.name = DECO
        shp.fill.background()
        shp.line.color.rgb = rgb(color)
        shp.line.width = Pt(width)
        shp.shadow.inherit = False
        return shp

    def icon(self, name: str, x: float, y: float, size: float, color: str) -> None:
        k = size / 24.0
        for pl in ICONS[name]:
            self.polyline([(x + px * k, y + py * k) for px, py in pl], color, 1.5)

    def picture(self, path: Path, x: float, y: float, w: float, h: float, name: str) -> Any:
        pic = self.slide.shapes.add_picture(str(path), emu(x), emu(y), emu(w), emu(h))
        pic.name = name
        return pic


# ---------- chrome ----------

def _brand_line(brand: dict) -> str:
    return "  ｜  ".join(v for v in (brand.get("org_name"), brand.get("footer_signature")) if v)


def _footer(cv: Canvas, content: dict, style: dict, ctx: Ctx) -> None:
    t, m = ctx.theme, ctx.m
    brand = content.get("brand") or {}
    line1 = _brand_line(brand)
    disc = brand.get("disclaimer", "")
    n_disc = m.lines(disc, BODY_W, t.caption) if disc else 0
    text_h = (lh(t.caption) if line1 else 0) + n_disc * lh(t.caption)
    if not text_h:
        return
    top = FOOTER_BOTTOM - text_h
    cv.rect(MARGIN_X, top - t.gap_s, BODY_W, 0.75, t.line)
    align = style["footer.align"]
    y = top
    if line1:
        cv.text(MARGIN_X, y, BODY_W, lh(t.caption), line1, t.caption, t.muted, align=align, name=CHROME)
        y += lh(t.caption)
    if disc:
        cv.text(MARGIN_X, y, BODY_W, n_disc * lh(t.caption), disc, t.caption, t.muted, align=align, name=CHROME)


def _header(cv: Canvas, page: Page, number: str, style: dict, ctx: Ctx) -> None:
    t = ctx.theme
    cv.rect(0, 0, SLIDE_W * 0.6, 4, t.primary)
    cv.rect(SLIDE_W * 0.6, 0, SLIDE_W * 0.4, 4, t.accent)
    x = MARGIN_X
    if style["section.icons"]:
        cv.rect(MARGIN_X, 22, 28, 28, t.primary, rounded=True)
        cv.icon(page.icon, MARGIN_X + 5, 27, 18, on_color(t.primary))
        x = MARGIN_X + 40
    title_w = SLIDE_W - MARGIN_X - x - 100
    cv.text(x, 36 - lh(t.title) / 2, title_w, lh(t.title), page.title, t.title, t.ink, bold=True, name=CHROME)
    if style["footer.page_number"]:
        cv.text(SLIDE_W - MARGIN_X - 90, 36 - lh(t.caption) / 2, 90, lh(t.caption), number, t.caption, t.muted,
                align="right", name=DECO)
    cv.rect(MARGIN_X, 62, BODY_W, 0.75, t.line)


def _check_titles(content: dict, ctx: Ctx, style: dict) -> None:
    t, m = ctx.theme, ctx.m
    title_w = SLIDE_W - 2 * MARGIN_X - 100 - (40 if style["section.icons"] else 0)
    for si, sec in enumerate(content["sections"]):
        if m.lines(sec["title"] + "（续）", title_w, t.title, True) > 1:
            raise LayoutError(f"sections[{si}].title", "章节标题过长，页标题只能一行，请缩短")


# ---------- brand placement ----------

def _logo_path(content: dict, base_dir: Path) -> Path | None:
    raw = (content.get("brand") or {}).get("logo_path")
    if not raw:
        return None
    p = Path(raw)
    return p if p.is_absolute() else base_dir / p


def _logo_size(path: Path) -> tuple[float, float] | None:
    from PIL import Image as PILImage

    try:
        with PILImage.open(path) as im:
            iw, ih = im.size
    except (OSError, ValueError):
        return None
    k = min(120 / iw, 44 / ih)
    return iw * k, ih * k


def _anchor(pos: str, w: float, h: float, box: tuple[float, float, float, float]) -> tuple[float, float]:
    x0, y0, x1, y1 = box
    return {
        "top-left": (x0, y0),
        "top-right": (x1 - w, y0),
        "bottom-left": (x0, y1 - h),
        "bottom-right": (x1 - w, y1 - h),
        "center": ((x0 + x1 - w) / 2, y0),
    }[pos]


def _place_brand(cv: Canvas, content: dict, style: dict, ctx: Ctx, base_dir: Path, color: str,
                 box: tuple[float, float, float, float], warnings: list[str]) -> None:
    t, m = ctx.theme, ctx.m
    brand = content.get("brand") or {}
    logo_pos, org_pos = style["brand.logo_position"], style["brand.org_position"]
    logo_box: tuple[float, float, float, float] | None = None
    path = _logo_path(content, base_dir)
    if path is not None:
        size = _logo_size(path) if path.is_file() else None
        if size is None:
            warnings.append(f"LOGO 文件不存在或无法读取，封面只保留机构名称：{brand.get('logo_path')}")
        else:
            lw, lh_ = size
            x, y = _anchor(logo_pos, lw, lh_, box)
            cv.picture(path, x, y, lw, lh_, "hpr:logo")
            logo_box = (x, y, lw, lh_)
    org = brand.get("org_name")
    if not org:
        return
    ow = min(m.width(org, t.body, True) * 1.2 + 4, 360.0)
    oh = m.lines(org, ow, t.body, True) * lh(t.body)
    x, y = _anchor(org_pos, ow, oh, box)
    if logo_box and org_pos == logo_pos:
        lx, ly, lw, lh_ = logo_box
        y = ly + (lh_ - oh) / 2
        if org_pos in ("top-right", "bottom-right"):
            x = lx - 12 - ow
        elif org_pos == "center":
            y = ly + lh_ + 8
        else:
            x = lx + lw + 12
    cv.text(x, y, ow, oh, org, t.body, color, bold=True, name=CHROME)


# ---------- cover / toc / end ----------

def _meta_items(content: dict) -> list[tuple[str, str]]:
    items = [("客户", content["client"]["name"])]
    items += [(f["label"], f["value"]) for f in content["client"].get("facts", [])]
    if content.get("data_basis"):
        items.append(("数据依据", content["data_basis"]))
    items.append(("生成日期", content["generated_at"]))
    mgr = content.get("manager")
    if mgr:
        items.append((mgr.get("title") or "负责人", mgr["name"]))
    return items


def _cover_title(cv: Canvas, content: dict, ctx: Ctx, x: float, y: float, w: float, color: str,
                 sub_color: str, max_lines: int) -> float:
    t, m = ctx.theme, ctx.m
    size = t.title + 12
    n = m.lines(content["title"], w, size, True)
    if n > max_lines:
        raise LayoutError("title", f"方案名称过长（封面最多 {max_lines} 行），请缩短")
    cv.text(x, y, w, n * lh(size), content["title"], size, color, bold=True, name=CHROME)
    y += n * lh(size) + ctx.theme.gap_m
    if content.get("subtitle"):
        k = m.lines(content["subtitle"], w, t.heading)
        cv.text(x, y, w, k * lh(t.heading), content["subtitle"], t.heading, sub_color, name=CHROME)
        y += k * lh(t.heading)
    return y


def _meta_grid(cv: Canvas, items: list[tuple[str, str]], ctx: Ctx, x: float, bottom: float, w: float,
               label_color: str, value_color: str, rule: str) -> None:
    t, m = ctx.theme, ctx.m
    per = 4
    cw = (w - (per - 1) * t.gap_l) / per
    rows = [items[i:i + per] for i in range(0, len(items), per)]
    heights = [max(lh(t.caption) + m.lines(v, cw, t.body, True) * lh(t.body) for _, v in row) for row in rows]
    total = sum(heights) + t.gap_m * (len(rows) - 1)
    if len(rows) > 3:
        raise LayoutError("client.facts", "封面信息过多（最多 12 项），请精简或移到正文")
    y = bottom - total
    cv.rect(x, y - t.gap_m, w, 0.75, rule)
    for row, rh in zip(rows, heights, strict=True):
        for i, (label, value) in enumerate(row):
            cx = x + i * (cw + t.gap_l)
            cv.text(cx, y, cw, lh(t.caption), label, t.caption, label_color, name=CHROME)
            vh = rh - lh(t.caption)
            cv.text(cx, y + lh(t.caption), cw, vh, value, t.body, value_color, bold=True, name=CHROME)
        y += rh + t.gap_m


def _bg(slide: Any, color: str) -> None:
    slide.background.fill.solid()
    slide.background.fill.fore_color.rgb = rgb(color)


def _cover(prs: Any, content: dict, style: dict, ctx: Ctx, base_dir: Path, warnings: list[str]) -> None:
    t = ctx.theme
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    cv = Canvas(slide, t)
    variant = style["cover.variant"]
    bottom_brand = style["brand.logo_position"].startswith("bottom") or style["brand.org_position"].startswith("bottom")
    meta_bottom = SLIDE_H - 40 - (60 if bottom_brand else 0)
    if variant == "split":
        panel_w = SLIDE_W * 0.42
        _bg(slide, t.background)
        cv.rect(0, 0, panel_w, SLIDE_H, t.primary)
        fg = on_color(t.primary)
        _cover_title(cv, content, ctx, 40, 150, panel_w - 80, fg, mix(fg, t.primary, 0.25), 4)
        _place_brand(cv, content, style, ctx, base_dir, t.ink, (panel_w + 40, 32, SLIDE_W - 40, SLIDE_H - 24), warnings)
        _meta_grid(cv, _meta_items(content), ctx, panel_w + 40, meta_bottom, SLIDE_W - panel_w - 80,
                   t.muted, t.ink, t.line)
        return
    if variant == "minimal":
        _bg(slide, t.background)
        cv.rect(0, 0, SLIDE_W, 6, t.primary)
        y = _cover_title(cv, content, ctx, 56, 170, 620, t.ink, t.muted, 3)
        cv.rect(56, y + t.gap_m, 64, 4, t.accent)
        _place_brand(cv, content, style, ctx, base_dir, t.primary, (56, 32, SLIDE_W - 56, SLIDE_H - 24), warnings)
        _meta_grid(cv, _meta_items(content), ctx, 56, meta_bottom, SLIDE_W - 112, t.muted, t.ink, t.line)
        return
    _bg(slide, t.primary)
    fg = on_color(t.primary)
    soft = mix(t.primary, fg, 0.25)
    for r in (230.0, 175.0, 120.0):
        cv.oval(800 - r, 280 - r, 2 * r, 2 * r, None, line=soft)
    cv.polyline([(430, 390), (520, 376), (575, 398), (630, 348), (685, 358), (740, 300), (805, 310), (955, 250)],
                t.accent, 2.2)
    _cover_title(cv, content, ctx, 56, 150, 520, fg, mix(fg, t.primary, 0.25), 3)
    _place_brand(cv, content, style, ctx, base_dir, fg, (56, 32, SLIDE_W - 56, SLIDE_H - 24), warnings)
    _meta_grid(cv, _meta_items(content), ctx, 56, meta_bottom, 560, soft, fg, soft)


def _show_toc(content: dict, style: dict) -> bool:
    return style["toc"] == "on" or (style["toc"] == "auto" and len(content["sections"]) >= 8)


def _toc(prs: Any, content: dict, style: dict, ctx: Ctx) -> None:
    t, m = ctx.theme, ctx.m
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    _bg(slide, t.background)
    cv = Canvas(slide, t)
    cv.rect(0, 0, SLIDE_W * 0.6, 4, t.primary)
    cv.rect(SLIDE_W * 0.6, 0, SLIDE_W * 0.4, 4, t.accent)
    cv.text(MARGIN_X, 36 - lh(t.title) / 2, 400, lh(t.title), "目录", t.title, t.ink, bold=True, name=DECO)
    cv.rect(MARGIN_X, 62, BODY_W, 0.75, t.line)
    col_w = (BODY_W - t.gap_l) / 2
    per_col = (len(content["sections"]) + 1) // 2
    for i, sec in enumerate(content["sections"]):
        col, row = divmod(i, per_col)
        x = MARGIN_X + col * (col_w + t.gap_l)
        y = BODY_TOP + 10 + row * (lh(t.heading) + t.gap_m)
        cv.text(x, y, 44, lh(t.heading), f"{i + 1:02d}", t.heading, t.primary, bold=True, name=DECO)
        if m.lines(sec["title"], col_w - 52, t.heading) > 1:
            raise LayoutError(f"sections[{i}].title", "章节标题过长，目录只能一行，请缩短")
        cv.text(x + 52, y, col_w - 52, lh(t.heading), sec["title"], t.heading, t.ink, name=CHROME)
    _footer(cv, content, style, ctx)


def _end(prs: Any, content: dict, ctx: Ctx) -> None:
    t, m = ctx.theme, ctx.m
    brand = content.get("brand") or {}
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    _bg(slide, t.primary)
    cv = Canvas(slide, t)
    fg = on_color(t.primary)
    y = 200.0
    org = brand["org_name"]
    n = m.lines(org, 700, t.title, True)
    cv.text(130, y, 700, n * lh(t.title), org, t.title, fg, bold=True, align="center", name=CHROME)
    y += n * lh(t.title) + t.gap_m
    if brand.get("footer_signature"):
        cv.text(130, y, 700, lh(t.heading), brand["footer_signature"], t.heading, mix(fg, t.primary, 0.25),
                align="center", name=CHROME)
    if brand.get("disclaimer"):
        k = m.lines(brand["disclaimer"], BODY_W, t.caption)
        cv.text(MARGIN_X, FOOTER_BOTTOM - k * lh(t.caption), BODY_W, k * lh(t.caption), brand["disclaimer"],
                t.caption, mix(fg, t.primary, 0.25), align="center", name=CHROME)


# ---------- primitives ----------

def _tone(t: Theme, tone: str) -> str:
    return getattr(t, _TONE.get(tone, "muted"))


def _draw_grid(cv: Canvas, pl: Placed, g: CardGrid, ctx: Ctx) -> None:
    t = ctx.theme
    cw = grid_col_w(g, pl.w, ctx)
    y = pl.y
    for row in grid_rows(g):
        rh = max(card_height(c, cw, ctx) for c in row)
        for i, card in enumerate(row):
            x = pl.x + i * (cw + t.gap_m)
            stripe = t.out if card.tag and card.tag.tone in ("out", "alert") else t.primary
            cv.rect(x, y, cw, rh, t.background, line=t.line)
            cv.rect(x, y, cw, STRIPE, stripe)
            cy = y + STRIPE + t.gap_m
            inner = cw - 2 * t.gap_m
            for part in card_parts(card, inner, ctx):
                if part.kind == "bar" and card.bar:
                    _range_bar(cv, x + t.gap_m, cy, inner, card.bar, t)
                else:
                    color = {"title": t.muted, "value": t.ink, "line": t.ink}.get(part.kind)
                    if part.kind == "tag" and card.tag:
                        color = _tone(t, card.tag.tone)
                    cv.text(x + t.gap_m, cy, inner, part.height, part.text, part.size, color or t.ink,
                            bold=part.bold)
                cy += part.height + t.gap_xs
        y += rh + t.gap_m


def _range_bar(cv: Canvas, x: float, y: float, w: float, bar: Any, t: Theme) -> None:
    span = bar.high - bar.low or 1.0
    dmin, dmax = bar.low - 0.6 * span, bar.high + 0.6 * span
    pos = (min(max(bar.value, dmin), dmax) - dmin) / (dmax - dmin)
    cv.rect(x, y + 3, w, 4, t.pale)
    cv.rect(x + w * (bar.low - dmin) / (dmax - dmin), y + 3, w * span / (dmax - dmin), 4, t.primary_soft)
    cv.rect(x + w * pos - 1, y, 2, 10, t.ink)


def _cell_border_bottom(cell: Any, color: str) -> None:
    tcpr = cell._tc.get_or_add_tcPr()
    for tag in ("a:lnL", "a:lnR", "a:lnT", "a:lnB"):
        old = tcpr.find(qn(tag))
        if old is not None:
            tcpr.remove(old)
    for i, tag in enumerate(("a:lnL", "a:lnR", "a:lnT", "a:lnB")):
        ln = OxmlElement(tag)
        if tag == "a:lnB":
            ln.set("w", str(int(0.75 * 12700)))
            fill = OxmlElement("a:solidFill")
            clr = OxmlElement("a:srgbClr")
            clr.set("val", color[1:])
            fill.append(clr)
            ln.append(fill)
        else:
            ln.set("w", "0")
            ln.append(OxmlElement("a:noFill"))
        tcpr.insert(i, ln)


def _draw_table(cv: Canvas, pl: Placed, tb: Table, ctx: Ctx) -> None:
    t = ctx.theme
    geo = table_geometry(tb, pl.w, ctx)
    gf = cv.slide.shapes.add_table(len(tb.rows) + 1, len(tb.columns), emu(pl.x), emu(pl.y), emu(pl.w), emu(pl.h))
    gf.name = BODY
    table = gf.table
    table.first_row = True
    table.horz_banding = False
    style_id = table._tbl.tblPr.find(qn("a:tableStyleId"))
    if style_id is None:
        style_id = OxmlElement("a:tableStyleId")
        table._tbl.tblPr.append(style_id)
    style_id.text = NO_GRID_STYLE
    for i, w in enumerate(geo.col_w):
        table.columns[i].width = emu(w)
    table.rows[0].height = emu(geo.header_h)
    for r, h in enumerate(geo.row_h, start=1):
        table.rows[r].height = emu(h)
    for r, cells in enumerate([tb.columns, *tb.rows]):
        for c, value in enumerate(cells):
            cell = table.cell(r, c)
            _cell_border_bottom(cell, t.line)
            cell.margin_left = cell.margin_right = emu(geo.pad_x)
            cell.margin_top = cell.margin_bottom = emu(geo.pad_y)
            cell.vertical_anchor = MSO_ANCHOR.TOP
            if r == 0:
                cell.fill.solid()
                cell.fill.fore_color.rgb = rgb(t.pale)
            else:
                cell.fill.background()
            highlight = r > 0 and tb.highlight_col == c and value not in ("", "—")
            color = t.primary if r == 0 else (t.out if highlight else t.ink)
            para = cell.text_frame.paragraphs[0]
            para.line_spacing = Pt(t.small * LINE)
            run = para.add_run()
            run.text = value
            _set_font(run, t, t.small, color, r == 0 or highlight)


def _draw_kv(cv: Canvas, pl: Placed, kv: KeyValue, ctx: Ctx) -> None:
    t = ctx.theme
    pair_w, label_w, heights = kv_geometry(kv, pl.w, ctx)
    y = pl.y
    for row, rh in zip(kv_rows(kv), heights, strict=True):
        for i, (k, v) in enumerate(row):
            x = pl.x + i * (pair_w + t.gap_l)
            cv.text(x, y + 2, label_w, rh - t.gap_s, k, t.small, t.muted)
            cv.text(x + label_w + t.gap_s, y, pair_w - label_w - t.gap_s, rh - t.gap_s, v, t.body, t.ink)
            cv.rect(x, y + rh - t.gap_s / 2, pair_w, 0.5, t.line)
        y += rh


def _draw_callout(cv: Canvas, pl: Placed, co: Callout, ctx: Ctx) -> None:
    t, m = ctx.theme, ctx.m
    bg = {"alert": t.pale_alert, "warn": t.pale_out}.get(co.tone, t.pale)
    edge = {"alert": t.alert, "warn": t.out}.get(co.tone, t.primary)
    cv.rect(pl.x, pl.y, pl.w, pl.h, bg, rounded=True)
    cv.rect(pl.x, pl.y, 4, pl.h, edge)
    x, y, w = pl.x + 4 + t.gap_m, pl.y + t.gap_m, pl.w - 4 - 2 * t.gap_m
    if co.title:
        cv.text(x, y, w, lh(t.body), co.title, t.body, edge, bold=True)
        y += lh(t.body) + t.gap_xs
    cv.text(x, y, w, m.lines(co.text, w, t.body) * lh(t.body), co.text, t.body, t.ink)


def _draw_chart(cv: Canvas, pl: Placed, ch: Chart, ctx: Ctx) -> None:
    t = ctx.theme
    if ch.kind != "donut":
        add_chart(cv.slide, ch, pl.x, pl.y, pl.w, pl.h, t)
        return
    size = min(pl.h, 180.0)
    add_chart(cv.slide, ch, pl.x, pl.y, size, size, t)
    if ch.center:
        cv.text(pl.x, pl.y + size / 2 - lh(t.heading) / 2, size, lh(t.heading), ch.center, t.heading, t.ink,
                bold=True, align="center")
    colors = donut_colors(t)
    lx = pl.x + size + t.gap_l
    ly = pl.y + (size - len(ch.legend) * (lh(t.body) + t.gap_xs)) / 2
    for i, line in enumerate(ch.legend):
        cv.rect(lx, ly + lh(t.body) / 2 - 5, 10, 10, colors[i % len(colors)])
        cv.text(lx + 18, ly, pl.w - size - t.gap_l - 18, lh(t.body), line, t.body, t.ink)
        ly += lh(t.body) + t.gap_xs


def _draw_timeline(cv: Canvas, pl: Placed, tl: Timeline, ctx: Ctx) -> None:
    t, m = ctx.theme, ctx.m
    y = pl.y
    for row in timeline_rows(tl):
        sw = (pl.w - (len(row) - 1) * t.gap_m) / len(row)
        rh = max(step_height(s, sw, ctx) for s in row)
        cv.rect(pl.x, y + 5.5, pl.w, 1, t.line)
        for i, step in enumerate(row):
            x = pl.x + i * (sw + t.gap_m)
            cv.oval(x, y, 12, 12, t.primary)
            ly = y + 12 + t.gap_s
            label_h = m.lines(step.label, sw, t.body, True) * lh(t.body)
            cv.text(x, ly, sw, label_h, step.label, t.body, t.ink, bold=True)
            if step.lines:
                by = ly + label_h + t.gap_xs
                cv.rect(x, by, sw, y + rh - by, t.pale, rounded=True)
                cy = by + t.gap_s
                for ln in step.lines:
                    h = m.lines(ln, sw - 2 * t.gap_s, t.small) * lh(t.small)
                    cv.text(x + t.gap_s, cy, sw - 2 * t.gap_s, h, ln, t.small, t.ink)
                    cy += h
        y += rh + t.gap_l


def _draw_columns(cv: Canvas, pl: Placed, cols: Columns, ctx: Ctx) -> None:
    t, m = ctx.theme, ctx.m
    y = pl.y
    for row in columns_rows(cols):
        cw = (pl.w - (len(row) - 1) * t.gap_m) / len(row)
        rh = max(column_height(c, cw, ctx) for c in row)
        for i, col in enumerate(row):
            x = pl.x + i * (cw + t.gap_m)
            color = _tone(t, col.tone) if col.tone != "neutral" else t.primary
            cv.rect(x, y, cw, rh, t.pale, rounded=True)
            cv.rect(x, y, cw, STRIPE, color)
            cy = y + STRIPE + t.gap_m
            cv.text(x + t.gap_m, cy, cw - 2 * t.gap_m, lh(t.heading), col.title, t.heading, color, bold=True)
            cy += lh(t.heading) + t.gap_s
            inner = cw - 2 * t.gap_m
            for item in col.items:
                h = m.lines(item, inner - t.small, t.small) * lh(t.small)
                cv.text(x + t.gap_m, cy, t.small, lh(t.small), "·", t.small, color, bold=True, name=DECO)
                cv.text(x + t.gap_m + t.small, cy, inner - t.small, h, item, t.small, t.ink)
                cy += h + t.gap_xs
        y += rh + t.gap_m


def _draw_media(cv: Canvas, pl: Placed, md: Media, ctx: Ctx, base_dir: Path, warnings: list[str]) -> None:
    t, m = ctx.theme, ctx.m
    cv.rect(pl.x, pl.y, pl.w, pl.h, t.pale, rounded=True)
    tx, tw = pl.x + t.gap_m, pl.w * 0.62 - 2 * t.gap_m
    cv.text(tx, pl.y + t.gap_m, tw, lh(t.heading), md.name, t.heading, t.primary, bold=True)
    dy = pl.y + t.gap_m + lh(t.heading) + t.gap_xs
    cv.text(tx, dy, tw, m.lines(md.description, tw, t.body) * lh(t.body), md.description, t.body, t.ink)
    bx = pl.x + pl.w * 0.62
    bw, bh = pl.w * 0.38 - t.gap_m, pl.h - 2 * t.gap_m
    by = pl.y + t.gap_m
    cv.rect(bx, by, bw, bh, t.background, line=t.line, rounded=True)
    label = f"查看示范/产品详情：{md.name}"
    link_h = m.lines(label, bw - 2 * t.gap_s, t.body, True) * lh(t.body)
    if md.media_path:
        path = Path(md.media_path)
        path = path if path.is_absolute() else base_dir / path
        movie_h = bh - link_h - 2 * t.gap_s
        if path.suffix.lower() == ".mp4" and path.is_file() and movie_h > 30:
            try:
                mv = cv.slide.shapes.add_movie(str(path), emu(bx + t.gap_s), emu(by + t.gap_s),
                                               emu(bw - 2 * t.gap_s), emu(movie_h), mime_type="video/mp4")
                mv.name = "hpr:media"
            except Exception as exc:  # noqa: BLE001 — any embed failure degrades to the link
                warnings.append(f"视频未能嵌入，已改为可点击链接：{md.name}（{exc}）")
        else:
            warnings.append(f"视频文件不可用，已改为可点击链接：{md.name}")
    cv.text(bx + t.gap_s, by + bh - link_h - t.gap_s, bw - 2 * t.gap_s, link_h, label, t.body, t.primary,
            bold=True, name=CHROME, link=md.url)


def _draw_image(cv: Canvas, pl: Placed, im: Image, ctx: Ctx) -> None:
    t, m = ctx.theme, ctx.m
    w, h = image_size(im, pl.w, ctx)
    path = Path(im.path)
    path = path if path.is_absolute() else ctx.base_dir / path
    cv.picture(path, pl.x, pl.y, w, h, "hpr:image")
    if im.caption:
        ch = m.lines(im.caption, pl.w, t.caption) * lh(t.caption)
        cv.text(pl.x, pl.y + h + t.gap_xs, pl.w, ch, im.caption, t.caption, t.muted)


def _minutes(hhmm: str) -> int:
    h, mm = (int(x) for x in hhmm.split(":"))
    total = h * 60 + mm
    return total + 24 * 60 if total < 18 * 60 else total


def _draw_timebars(cv: Canvas, pl: Placed, tb: TimeBars, ctx: Ctx) -> None:
    t, m = ctx.theme, ctx.m
    start, span = 18 * 60, 18 * 60
    label_w = 56.0
    ax, aw = pl.x + label_w, pl.w - label_w
    y = pl.y
    for i, (label, bed, wake) in enumerate(tb.rows):
        rh = lh(t.body)
        cv.text(pl.x, y, label_w - 8, rh, label, t.body, t.muted, bold=True, name=DECO)
        b, w_ = _minutes(bed), _minutes(wake)
        if w_ <= b:
            w_ += 24 * 60
        x0 = ax + aw * max(0, min(span, b - start)) / span
        x1 = ax + aw * max(0, min(span, w_ - start)) / span
        color = t.primary if i == len(tb.rows) - 1 else t.primary_soft
        cv.rect(x0, y + 2, max(4.0, x1 - x0), rh - 4, color, rounded=True)
        text = f"{bed} – {wake}"
        tw = m.width(text, t.small, True) * 1.2 + 4
        inside = tw <= (x1 - x0) - 8
        tx = x0 + 6 if inside else min(x1 + 6, pl.x + pl.w - tw)
        cv.text(tx, y + (rh - lh(t.small)) / 2, tw, lh(t.small), text, t.small,
                on_color(color) if inside else t.ink, bold=True)
        y += rh + t.gap_s
    for k in range(0, 7):
        mins = start + k * 180
        hh = (mins // 60) % 24
        tx = ax + aw * k / 6 - 16
        cv.text(max(pl.x, min(tx, pl.x + pl.w - 32)), y, 32, lh(t.caption), f"{hh:02d}:00", t.caption, t.muted,
                align="center", name=DECO)


def _draw(cv: Canvas, pl: Placed, ctx: Ctx, base_dir: Path, warnings: list[str]) -> None:
    t, m = ctx.theme, ctx.m
    p = pl.prim
    if isinstance(p, SubHeading):
        cv.rect(pl.x - 10, pl.y + lh(t.heading) * 0.2, 3, lh(t.heading) * 0.6, t.accent)
        cv.text(pl.x, pl.y, pl.w, pl.h - t.gap_xs, p.text, t.heading, t.ink, bold=True)
    elif isinstance(p, Paragraph):
        pad = t.gap_m if p.boxed else 0.0
        if p.boxed:
            cv.rect(pl.x, pl.y, pl.w, pl.h, t.pale, rounded=True)
        cv.text(pl.x + pad, pl.y + pad, pl.w - 2 * pad, pl.h - 2 * pad, p.text, t.body, t.ink)
    elif isinstance(p, Bullets):
        indent = t.body * 1.4
        y = pl.y
        for n, item in enumerate(p.items, start=1):
            h = m.lines(item, pl.w - indent, t.body) * lh(t.body)
            marker = {"numbers": f"{n}.", "checks": "✓"}.get(p.style, "•")
            cv.text(pl.x, y, indent, lh(t.body), marker, t.body, t.primary, bold=True, name=DECO)
            cv.text(pl.x + indent, y, pl.w - indent, h, item, t.body, t.ink)
            y += h + t.gap_xs
    elif isinstance(p, CardGrid):
        _draw_grid(cv, pl, p, ctx)
    elif isinstance(p, Table):
        _draw_table(cv, pl, p, ctx)
    elif isinstance(p, KeyValue):
        _draw_kv(cv, pl, p, ctx)
    elif isinstance(p, Callout):
        _draw_callout(cv, pl, p, ctx)
    elif isinstance(p, Chart):
        _draw_chart(cv, pl, p, ctx)
    elif isinstance(p, Timeline):
        _draw_timeline(cv, pl, p, ctx)
    elif isinstance(p, Columns):
        _draw_columns(cv, pl, p, ctx)
    elif isinstance(p, Media):
        _draw_media(cv, pl, p, ctx, base_dir, warnings)
    elif isinstance(p, Image):
        _draw_image(cv, pl, p, ctx)
    elif isinstance(p, TimeBars):
        _draw_timebars(cv, pl, p, ctx)
    else:
        raise TypeError(type(p).__name__)


def render_pptx(content: dict, style: dict, out_path: Path, base_dir: Path, m: Measurer) -> list[str]:
    theme = build_theme(style, "pptx")
    ctx = make_ctx(content, theme, m, base_dir)
    _check_titles(content, ctx, style)
    pages = paginate(section_prims(content, style), ctx)
    warnings: list[str] = []
    prs = Presentation()
    prs.slide_width, prs.slide_height = Emu(12192000), Emu(6858000)
    toc = _show_toc(content, style)
    end = bool((content.get("brand") or {}).get("org_name"))
    total = 1 + int(toc) + len(pages) + int(end)
    _cover(prs, content, style, ctx, base_dir, warnings)
    if toc:
        _toc(prs, content, style, ctx)
    first_number = 2 + int(toc)
    for i, page in enumerate(pages):
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        _bg(slide, theme.background)
        cv = Canvas(slide, theme)
        _header(cv, page, f"{first_number + i} / {total}", style, ctx)
        for pl in page.placed:
            _draw(cv, pl, ctx, base_dir, warnings)
        _footer(cv, content, style, ctx)
    if end:
        _end(prs, content, ctx)
    prs.save(str(out_path))
    return warnings

```

- [ ] **Step 5: 写 `hpr/qa.py`（PPT 部分）**

```python
"""Hard QA gates: every required text present, nothing overflows its box, nothing outside the
page, readable contrast (spec §7.2)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from hpr.measure import LINE, Measurer
from hpr.style import INK, contrast
from hpr.theme import Theme, on_color

_EMU_PER_PT = 12700
_TEXT_NAMES = ("hpr:body", "hpr:chrome")


def norm(s: str) -> str:
    return re.sub(r"\s+", "", s)


def missing_texts(required: list[str], corpus: str) -> list[str]:
    c = norm(corpus)
    out: list[str] = []
    for r in required:
        if norm(r) not in c and r not in out:
            out.append(r)
    return out


def _shape_texts(sh: Any) -> list[str]:
    if sh.has_text_frame:
        return [sh.text_frame.text]
    if getattr(sh, "has_table", False) and sh.has_table:
        return [cell.text for row in sh.table.rows for cell in row.cells]
    return []


def pptx_corpus(path: Path) -> str:
    from pptx import Presentation

    body: list[str] = []
    chrome: list[str] = []
    for slide in Presentation(str(path)).slides:
        for sh in slide.shapes:
            if sh.name == "hpr:body":
                body += _shape_texts(sh)
            elif sh.name == "hpr:chrome":
                chrome += _shape_texts(sh)
    return "".join(body) + "\x00" + "\x00".join(chrome)


def pptx_bounds(path: Path) -> list[str]:
    from pptx import Presentation

    prs = Presentation(str(path))
    sw, sh_ = prs.slide_width, prs.slide_height
    tol = _EMU_PER_PT
    issues = []
    for n, slide in enumerate(prs.slides, start=1):
        for sh in slide.shapes:
            if None in (sh.left, sh.top, sh.width, sh.height):
                continue
            if sh.left < -tol or sh.top < -tol or sh.left + sh.width > sw + tol or sh.top + sh.height > sh_ + tol:
                issues.append(f"第 {n} 页形状「{sh.name}」超出页面")
    return issues


def _para_need(para: Any, width_pt: float, m: Measurer) -> float:
    runs = list(para.runs)
    size = (runs[0].font.size.pt if runs and runs[0].font.size else 14.0)
    bold = bool(runs and runs[0].font.bold)
    return m.lines(para.text, width_pt, size, bold) * size * LINE


def pptx_overflow(path: Path, m: Measurer) -> list[str]:
    from pptx import Presentation

    issues = []
    for n, slide in enumerate(Presentation(str(path)).slides, start=1):
        for sh in slide.shapes:
            if sh.name not in _TEXT_NAMES:
                continue
            if sh.has_text_frame:
                need = sum(_para_need(p, sh.width / _EMU_PER_PT, m) for p in sh.text_frame.paragraphs)
                if need > sh.height / _EMU_PER_PT + 0.5:
                    issues.append(f"第 {n} 页「{sh.text_frame.text[:16]}」需要 {need:.0f}pt，框高 {sh.height / _EMU_PER_PT:.0f}pt")
            elif getattr(sh, "has_table", False) and sh.has_table:
                tbl = sh.table
                for r, row in enumerate(tbl.rows):
                    for c, cell in enumerate(row.cells):
                        w = (tbl.columns[c].width - cell.margin_left - cell.margin_right) / _EMU_PER_PT
                        need = sum(_para_need(p, w, m) for p in cell.text_frame.paragraphs)
                        need += (cell.margin_top + cell.margin_bottom) / _EMU_PER_PT
                        if need > row.height / _EMU_PER_PT + 0.5:
                            issues.append(f"第 {n} 页表格第 {r + 1} 行第 {c + 1} 列溢出")
    return issues


def contrast_report(t: Theme) -> dict[str, Any]:
    ratios = {
        "ink_on_background": round(contrast(INK, t.background), 2),
        "primary_on_background": round(contrast(t.primary, t.background), 2),
        "text_on_primary": round(contrast(on_color(t.primary), t.primary), 2),
    }
    ok = ratios["ink_on_background"] >= 7 and ratios["primary_on_background"] >= 4.5 and ratios["text_on_primary"] >= 4.5
    return {"ok": ok, **ratios}


def qa_pptx(path: Path, required: list[str], theme: Theme, m: Measurer) -> dict[str, Any]:
    from pptx import Presentation

    missing = missing_texts(required, pptx_corpus(path))
    overflow = pptx_overflow(path, m)
    bounds = pptx_bounds(path)
    cr = contrast_report(theme)
    passed = not missing and not overflow and not bounds and cr["ok"]
    return {
        "status": "passed" if passed else "failed",
        "slides": len(Presentation(str(path)).slides),
        "missing": missing,
        "overflow": overflow,
        "out_of_bounds": bounds,
        "contrast": cr,
    }
```

- [ ] **Step 6: 写 `scripts/render.py`**

```python
#!/usr/bin/env python3
"""Render a health-plan content JSON to PPTX (and, from a later version, PDF) with QA gates."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(1, str(Path(__file__).resolve().parents[2] / "shared"))

from _cli import emit_json
from hpr.blocks import RenderError
from hpr.content import iter_blocks, load_content, required_texts, validate_content
from hpr.measure import Measurer
from hpr.ppt_draw import render_pptx
from hpr.ppt_layout import LayoutError
from hpr.qa import qa_pptx
from hpr.style import NOT_APPLIED, Layer, load_layer, resolve
from hpr.theme import build_theme

_BASENAME = re.compile(r"^[^/\\\x00]{1,120}$")


def _fail(errors: list[str]) -> int:
    emit_json({"ok": False, "errors": errors})
    return 1


def _missing_images(content: dict, base_dir: Path) -> list[str]:
    errs = []
    for si, _sid, bi, blk in iter_blocks(content):
        if blk["kind"] == "image":
            p = Path(blk["path"])
            p = p if p.is_absolute() else base_dir / p
            if not p.is_file():
                errs.append(f"sections[{si}].blocks[{bi - 1}]: 找不到图片文件：{blk['path']}")
    return errs


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--content", required=True)
    ap.add_argument("--style", action="append", default=[], help="样式层文件，按优先级从高到低重复传入")
    ap.add_argument("--format", choices=("pptx", "pdf", "both"))
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--basename", required=True)
    args = ap.parse_args(argv)
    if not _BASENAME.match(args.basename) or args.basename in (".", ".."):
        return _fail(["basename 只能是文件名，不能包含路径分隔符"])
    try:
        content = load_content(Path(args.content))
        layers = [load_layer(Path(p)) for p in args.style]
    except ValueError as exc:
        return _fail([str(exc)])
    errs = validate_content(content)
    if errs:
        return _fail([str(e) for e in errs])
    base_dir = Path(args.content).resolve().parent
    img_errs = _missing_images(content, base_dir)
    if img_errs:
        return _fail(img_errs)
    if args.format:
        layers.insert(0, Layer("命令行", {"output.formats": args.format}))
    res = resolve(layers, content)
    style = res.style
    formats = ["pptx", "pdf"] if style["output.formats"] == "both" else [style["output.formats"]]
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    targets = {f: out_dir / f"{args.basename}.{f}" for f in formats}
    existing = [str(p) for p in targets.values() if p.exists()]
    if existing:
        return _fail([f"文件已存在，不覆盖：{p}（请换一个 basename，例如用新的生成时间）" for p in existing])
    if "pdf" in formats:
        return _fail(["PDF 输出在本版本尚未提供，请先用 --format pptx"])
    m = Measurer()
    required = required_texts(content)
    files: dict[str, str] = {}
    qa: dict[str, dict] = {}
    warnings: list[str] = []
    try:
        warnings += render_pptx(content, style, targets["pptx"], base_dir, m)
    except (RenderError, LayoutError) as exc:
        return _fail([str(exc)])
    qa["pptx"] = qa_pptx(targets["pptx"], required, build_theme(style, "pptx"), m)
    files["pptx"] = str(targets["pptx"])
    ok = all(q["status"] == "passed" for q in qa.values())
    if not ok:
        for fmt, q in qa.items():
            if q["status"] != "passed":
                bad = out_dir / f"{args.basename}.qa-failed.{fmt}"
                Path(files[fmt]).rename(bad)
                files[fmt] = str(bad)
    (out_dir / f"{args.basename}.qa.json").write_text(json.dumps(qa, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / f"{args.basename}.params-report.json").write_text(
        json.dumps(res.report, ensure_ascii=False, indent=2), encoding="utf-8")
    emit_json({
        "ok": ok,
        "files": files,
        "qa": qa,
        "not_applied": [e for e in res.report if e["status"] in NOT_APPLIED],
        "warnings": warnings,
        "errors": [],
    })
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 7: 运行测试，逐个修到绿**

Run: `uv run --no-sync pytest platform-skills/tests/health_plan_report -q`
Expected: 全部 PASS。

这一步是整个计划里最可能需要调试的地方。允许调整的只有**绘制细节**（坐标微调、颜色选择），不允许的是：缩小字号、改 `SAFETY`/`LINE`、让质检放水、删断言。常见坑：
- `pptx_overflow` 报某框溢出：说明该框高度不是从 `ppt_layout` 的几何函数来的——回到对应 `_draw_*`，高度改用同一函数的结果。
- `test_cjk_font_on_every_run` 红：某处直接写了 `cell.text = ...` 或 `tf.text = ...`（会建出没设字体的 run）——一律走 `Canvas.text` / `_set_font`。
- 表格：`table._tbl.tblPr` 在 python-pptx 1.0.2 可用；若属性名不同，以 `gf._element.graphic.graphicData.tbl.tblPr` 取。

break → red → restore → green：
- `pptx_corpus` 改为只收 `hpr:chrome` → `test_every_required_text_is_found_and_detector_bites` 红；还原。
- `render.py` 删掉「文件已存在」检查 → `test_existing_output_is_not_overwritten` 红；还原。
- `_set_font` 删掉 `ea.set(...)` → `test_cjk_font_on_every_run` 红；还原。

- [ ] **Step 8: lint 与提交**

```bash
uv run ruff format platform-skills/health-plan-report platform-skills/tests/health_plan_report
uv run ruff check platform-skills && uv run ruff format --check platform-skills
git add platform-skills/health-plan-report/scripts platform-skills/tests/health_plan_report
git commit -m "feat(health-plan-report): PPTX drawing, native charts, QA gates and render CLI"
```

---

### Task 6: 设计确认检查点（控制方执行，非子 Agent）

**目的**：在写 PDF 之前，让产品确认 PPT 的实际观感；此后视觉改动只改主题 token 与 `ppt_draw` 的绘制细节。

- [ ] **Step 1: 在真实沙箱镜像里渲染样例**（真实字体测量）

```bash
export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock
IMG=crpi-sgadimluo7wm655m.cn-hangzhou.personal.cr.aliyuncs.com/expert-work/sandbox:7ac31957
OUT=/private/tmp/claude-501/-Users-mac-src-github-jone-qian-expert-work/c8d39282-34dd-4791-b489-4f59e02f59b0/scratchpad/hpd/t6
mkdir -p $OUT && cp platform-skills/health-plan-report/sample/sample-plan.json $OUT/
python3 -c "from PIL import Image; Image.new('RGB',(640,360),'#DDEEEE').save('$OUT/trend-note.png')"
docker run --rm --platform linux/amd64 -v $PWD/platform-skills:/ps:ro -v $OUT:/w -w /w --entrypoint sh $IMG -c '
  python /ps/health-plan-report/scripts/render.py --content sample-plan.json --out-dir /w --basename sample &&
  soffice --headless --convert-to pdf --outdir /w /w/sample.pptx >/dev/null 2>&1'
pdftoppm -r 60 -png $OUT/sample.pdf $OUT/p
```

Expected: `render.py` 输出 `"ok": true`；`sample.pdf`（LibreOffice 转出的预览）生成。

- [ ] **Step 2: 拼缩略图给产品看**，同时列出：页数、每页用途、与浏览器设计稿 v2 方向 A 的差异。
- [ ] **Step 3: 收集意见，只改 token / 绘制细节**（例如间距档、卡片描边、封面图形位置），每改一次重跑 Step 1 与本地测试。
- [ ] **Step 4: 产品确认后，在 ledger 记录「T6 设计确认：<日期>，<确认人原话>」**，进入 T7。

---
### Task 7: PDF 输出（HTML/CSS + 内联 SVG → weasyprint）+ PDF 质检 + 镜像内测试

**Files:**
- Create: `platform-skills/health-plan-report/scripts/hpr/pdf_svg.py`
- Create: `platform-skills/health-plan-report/scripts/hpr/pdf_html.py`
- Modify: `platform-skills/health-plan-report/scripts/hpr/qa.py`（加 PDF 部分）
- Modify: `platform-skills/health-plan-report/scripts/render.py`（去掉「PDF 尚未提供」，接入 PDF）
- Create: `platform-skills/tests/health_plan_report/test_pdf.py`
- Modify: `platform-skills/tests/health_plan_report/test_render_pptx.py`（删除 `test_pdf_not_yet_available_message`）
- Create: `platform-skills/tests/in_image/case_hpr_render.py`

**Interfaces:**
- Consumes: `hpr.blocks.section_prims`、`hpr.prims.*`、`hpr.theme`、`hpr.icons.ICONS / icon_for_section`、`hpr.style.mix`
- Produces:
  - `hpr.pdf_svg.line_svg(prim: Chart, t: Theme) -> str`、`bar_svg(prim, t) -> str`、`donut_svg(prim, t) -> str`、`icon_svg(name: str, color: str, size_mm: float) -> str`、`timebar_svg(bed: str, wake: str, color: str, t: Theme) -> str`、`cover_deco_svg(t: Theme, fg: str) -> str`
  - `hpr.pdf_html.css_string(s: str) -> str`（CSS 字符串字面量，含引号）、`build_html(content, style, theme, base_dir) -> tuple[str, list[str]]`（HTML 与警告）、`render_pdf(content, style, out_path, base_dir) -> list[str]`
  - `hpr.qa.PAGE_NO_RE`（`第\d+页/共\d+页`，作用于去空白后的文本）、`pdf_text(path) -> str`、`pdf_fonts(path) -> list[str]`、`qa_pdf(path, content, required, theme) -> dict`（字段同 `qa_pptx`，`slides` 换成 `pages`，另有 `fonts_embedded: bool`）

- [ ] **Step 1: 写失败测试 `test_pdf.py`，并删掉 T5 的占位测试**

从 `test_render_pptx.py` 删除 `test_pdf_not_yet_available_message` 整个函数。

`test_pdf.py`：

```python
import importlib.util
import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path

import pytest
from hpr.content import required_texts
from hpr.pdf_html import build_html, css_string
from hpr.pdf_svg import bar_svg, cover_deco_svg, donut_svg, icon_svg, line_svg, timebar_svg
from hpr.prims import Chart
from hpr.qa import PAGE_NO_RE, missing_texts, norm
from hpr.style import Layer, resolve
from hpr.theme import build_theme
from PIL import Image as PILImage

RENDER = Path(__file__).resolve().parents[2] / "health-plan-report" / "scripts" / "render.py"
HAS_WEASY = importlib.util.find_spec("weasyprint") is not None


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("style", "svg"):
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in ("style", "svg"):
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def _visible_text(html: str) -> str:
    p = _Text()
    p.feed(html)
    return "".join(p.parts)


@pytest.fixture
def base(tmp_path):
    PILImage.new("RGB", (640, 360), "#DDEEEE").save(tmp_path / "trend-note.png")
    return tmp_path


def _html(sample, base, layers=()):
    style = resolve(list(layers), sample).style
    return build_html(sample, style, build_theme(style, "pdf"), base)


def test_every_required_text_is_in_visible_html(sample, base):
    html, warnings = _html(sample, base)
    assert missing_texts(required_texts(sample), _visible_text(html)) == []
    assert warnings == []


def test_no_scripts_and_font_declared(sample, base):
    html, _ = _html(sample, base)
    assert "<script" not in html.lower()
    assert "Noto Sans CJK SC" in html


def test_page_number_and_footer_options(sample, base):
    html, _ = _html(sample, base)
    assert 'counter(page)' in html and 'counter(pages)' in html
    html2, _ = _html(sample, base, [Layer("x", {"footer.page_number": "关", "footer.align": "居中"})])
    assert "counter(page)" not in html2
    assert "@bottom-center" in html2


@pytest.mark.parametrize("variant", ["band", "split", "minimal"])
def test_cover_variants(sample, base, variant):
    html, _ = _html(sample, base, [Layer("x", {"cover.variant": variant})])
    assert f'class="cover cover-{variant}' in html


def test_toc_rules(sample, base):
    assert 'class="toc"' in _html(sample, base)[0]  # 样例 8 个章节 ≥ 6
    assert 'class="toc"' not in _html(sample, base, [Layer("x", {"toc": "off"})])[0]


def test_css_string_escapes():
    assert css_string('a"b\\c\nd') == '"a\\"b\\\\c\\A d"'


def test_user_text_is_escaped(sample, base):
    sample["sections"][0]["blocks"][0]["items"][0]["text"] = "<b>x</b> & y"
    html, _ = _html(sample, base)
    assert "&lt;b&gt;x&lt;/b&gt; &amp; y" in html


def test_svgs_are_well_formed():
    t = build_theme(resolve([]).style, "pdf")
    line = Chart("line", ("09-01", "09-08", "09-15"), (6.8, 6.5, 6.2), "mmol/L", low=4.4, high=6.1)
    bar = Chart("bar", ("a", "b"), (1.0, 3.0))
    donut = Chart("donut", ("碳水", "蛋白", "脂肪"), (45.0, 23.0, 32.0), legend=("a", "b", "c"), center="1650 kcal")
    for svg in (line_svg(line, t), bar_svg(bar, t), donut_svg(donut, t), icon_svg("pulse", "#fff", 5),
                timebar_svg("23:00", "06:30", t.primary, t), cover_deco_svg(t, "#FFFFFF")):
        ET.fromstring(svg)


def test_page_number_regex_matches_normalised_footer():
    assert PAGE_NO_RE.search(norm("第 3 页 / 共 12 页"))


def test_missing_logo_warns(sample, base):
    sample["brand"]["logo_path"] = "nope.png"
    _, warnings = _html(sample, base)
    assert any("LOGO" in w for w in warnings)


@pytest.mark.skipif(HAS_WEASY, reason="本地无 weasyprint 时才测这条报错路径")
def test_pdf_without_weasyprint_is_a_clear_error(sample, base):
    (base / "plan.json").write_text(json.dumps(sample, ensure_ascii=False), encoding="utf-8")
    res = subprocess.run(
        [sys.executable, str(RENDER), "--content", str(base / "plan.json"), "--format", "pdf",
         "--out-dir", str(base / "o"), "--basename", "x"],
        capture_output=True, text=True, check=False,
    )
    assert res.returncode == 1
    assert "weasyprint" in json.loads(res.stdout)["errors"][0]
```

- [ ] **Step 2: 运行，确认失败**

Run: `uv run --no-sync pytest platform-skills/tests/health_plan_report/test_pdf.py -q`
Expected: `ModuleNotFoundError: No module named 'hpr.pdf_html'`

- [ ] **Step 3: 写 `hpr/pdf_svg.py`**

```python
"""Inline SVG for the PDF: charts, icons, sleep bars and cover decoration (theme colours)."""

from __future__ import annotations

from html import escape

from hpr.icons import ICONS
from hpr.prims import Chart
from hpr.style import mix
from hpr.theme import Theme


def _num(v: float) -> str:
    return f"{v:.1f}".rstrip("0").rstrip(".")


def _scale(values: list[float], lo_pad: float = 0.15) -> tuple[float, float]:
    lo, hi = min(values), max(values)
    pad = (hi - lo) * lo_pad or 1.0
    return lo - pad, hi + pad


def line_svg(prim: Chart, t: Theme) -> str:
    w, h, left, right, top, bottom = 600, 220, 42, 590, 12, 190
    vals = list(prim.values) + [v for v in (prim.low, prim.high) if v is not None]
    vmin, vmax = _scale(vals)
    n = len(prim.values)

    def px(i: int) -> float:
        return left + (right - left) * (i / (n - 1) if n > 1 else 0.5)

    def py(v: float) -> float:
        return bottom - (bottom - top) * (v - vmin) / (vmax - vmin)

    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="100%">']
    for k in range(5):
        v = vmin + (vmax - vmin) * k / 4
        y = py(v)
        out.append(f'<line x1="{left}" y1="{y:.1f}" x2="{right}" y2="{y:.1f}" stroke="{t.line}" stroke-width="0.6"/>')
        out.append(f'<text x="{left - 6}" y="{y + 3:.1f}" font-size="9" text-anchor="end" fill="{t.muted}">{_num(v)}</text>')
    if prim.low is not None and prim.high is not None:
        out.append(f'<rect x="{left}" y="{py(prim.high):.1f}" width="{right - left}" height="{py(prim.low) - py(prim.high):.1f}" '
                   f'fill="{t.within}" fill-opacity="0.12"/>')
    for bound in (prim.low, prim.high):
        if bound is not None:
            out.append(f'<line x1="{left}" y1="{py(bound):.1f}" x2="{right}" y2="{py(bound):.1f}" stroke="{t.within}" '
                       f'stroke-width="1" stroke-dasharray="4 3"/>')
    pts = " ".join(f"{px(i):.1f},{py(v):.1f}" for i, v in enumerate(prim.values))
    out.append(f'<polyline points="{pts}" fill="none" stroke="{t.primary}" stroke-width="2"/>')
    for i, v in enumerate(prim.values):
        out.append(f'<circle cx="{px(i):.1f}" cy="{py(v):.1f}" r="2.6" fill="{t.primary}"/>')
    last = prim.values[-1]
    out.append(f'<text x="{px(n - 1):.1f}" y="{py(last) - 8:.1f}" font-size="10" font-weight="700" text-anchor="end" '
               f'fill="{t.primary}">{_num(last)}</text>')
    for i in sorted({0, n // 2, n - 1}):
        out.append(f'<text x="{px(i):.1f}" y="{h - 10}" font-size="9" text-anchor="middle" fill="{t.muted}">'
                   f'{escape(prim.categories[i])}</text>')
    out.append("</svg>")
    return "".join(out)


def bar_svg(prim: Chart, t: Theme) -> str:
    w, h, left, right, top, bottom = 600, 220, 42, 590, 12, 190
    vmax = max(max(prim.values), 0) * 1.15 or 1.0
    n = len(prim.values)
    slot = (right - left) / n
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="100%">',
           f'<line x1="{left}" y1="{bottom}" x2="{right}" y2="{bottom}" stroke="{t.line}"/>']
    for i, v in enumerate(prim.values):
        bh = (bottom - top) * max(v, 0) / vmax
        x = left + i * slot + slot * 0.2
        out.append(f'<rect x="{x:.1f}" y="{bottom - bh:.1f}" width="{slot * 0.6:.1f}" height="{bh:.1f}" fill="{t.primary}"/>')
        out.append(f'<text x="{x + slot * 0.3:.1f}" y="{h - 10}" font-size="9" text-anchor="middle" fill="{t.muted}">'
                   f'{escape(prim.categories[i])}</text>')
    out.append("</svg>")
    return "".join(out)


def donut_colors(t: Theme) -> list[str]:
    return [t.primary, t.accent, t.primary_soft, mix(t.primary, t.background, 0.75), t.muted, t.within]


def donut_svg(prim: Chart, t: Theme) -> str:
    r, circ = 34.0, 2 * 3.141592653589793 * 34.0
    total = sum(prim.values) or 1.0
    colors = donut_colors(t)
    out = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100" width="100%">']
    offset = 0.0
    for i, v in enumerate(prim.values):
        seg = circ * v / total
        out.append(f'<circle cx="50" cy="50" r="{r}" fill="none" stroke="{colors[i % len(colors)]}" stroke-width="13" '
                   f'stroke-dasharray="{seg:.2f} {circ - seg:.2f}" stroke-dashoffset="{-offset:.2f}" '
                   f'transform="rotate(-90 50 50)"/>')
        offset += seg
    out.append("</svg>")
    return "".join(out)


def icon_svg(name: str, color: str, size_mm: float) -> str:
    lines = "".join(
        f'<polyline points="{" ".join(f"{x:.1f},{y:.1f}" for x, y in pl)}" fill="none" stroke="{color}" '
        f'stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>'
        for pl in ICONS[name]
    )
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="{size_mm}mm" '
            f'height="{size_mm}mm">{lines}</svg>')


def _minutes(hhmm: str) -> int:
    h, m = (int(x) for x in hhmm.split(":"))
    total = h * 60 + m
    return total + 24 * 60 if total < 18 * 60 else total


def timebar_svg(bed: str, wake: str, color: str, t: Theme) -> str:
    start, span = 18 * 60, 18 * 60
    b, w = _minutes(bed), _minutes(wake)
    if w <= b:
        w += 24 * 60
    x0 = 300 * max(0, min(span, b - start)) / span
    x1 = 300 * max(0, min(span, w - start)) / span
    ticks = "".join(
        f'<line x1="{300 * k / 6:.1f}" y1="0" x2="{300 * k / 6:.1f}" y2="14" stroke="{t.line}" stroke-width="0.5"/>'
        for k in range(7)
    )
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 300 14" width="100%">'
            f'<rect x="0" y="5" width="300" height="4" fill="{t.pale}"/>{ticks}'
            f'<rect x="{x0:.1f}" y="2" width="{max(3.0, x1 - x0):.1f}" height="10" rx="3" fill="{color}"/></svg>')


def cover_deco_svg(t: Theme, fg: str) -> str:
    soft = mix(t.primary, fg, 0.25)
    circles = "".join(f'<circle cx="160" cy="120" r="{r}" fill="none" stroke="{soft}" stroke-width="0.6"/>' for r in (110, 80, 50))
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 210 200" width="100%">{circles}'
            f'<polyline points="0,170 40,162 62,174 88,146 112,152 136,122 162,128 210,98" fill="none" '
            f'stroke="{t.accent}" stroke-width="1.6"/></svg>')
```

- [ ] **Step 4: 写 `hpr/pdf_html.py`**

```python
"""Build the A4 print HTML (weasyprint) for a plan: cover, optional TOC, flowing sections."""

from __future__ import annotations

from html import escape
from pathlib import Path
from typing import Any

from hpr.blocks import section_prims
from hpr.icons import icon_for_section
from hpr.pdf_svg import bar_svg, cover_deco_svg, donut_colors, donut_svg, icon_svg, line_svg, timebar_svg
from hpr.prims import (
    Bullets,
    Callout,
    CardGrid,
    Chart,
    Columns,
    Image,
    KeyValue,
    Media,
    Paragraph,
    Prim,
    SubHeading,
    Table,
    TimeBars,
    Timeline,
)
from hpr.theme import Theme, on_color

_POS_CLASS = {"top-left": "tl", "top-right": "tr", "bottom-left": "bl", "bottom-right": "br", "center": "ct"}


def e(s: str) -> str:
    return escape(s, quote=True)


def css_string(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\A ") + '"'


def _brand_line(brand: dict) -> str:
    return "  ｜  ".join(v for v in (brand.get("org_name"), brand.get("footer_signature")) if v)


def _css(content: dict, style: dict, t: Theme) -> str:
    brand = content.get("brand") or {}
    footer = "\n".join(x for x in (_brand_line(brand), brand.get("disclaimer", "")) if x)
    footer_box = "@bottom-center" if style["footer.align"] == "center" else "@bottom-left"
    page_no = ('@bottom-right { content: "第 " counter(page) " 页 / 共 " counter(pages) " 页"; '
               f"font-size: {t.caption}pt; color: {t.muted}; }}") if style["footer.page_number"] else ""
    fg = on_color(t.primary)
    return f"""
@page {{
  size: A4; margin: 22mm 18mm 28mm 18mm;
  @top-left {{ content: {css_string(content["title"])}; font-size: {t.caption}pt; color: {t.muted}; }}
  @top-right {{ content: {css_string(brand.get("org_name", ""))}; font-size: {t.caption}pt; color: {t.muted}; }}
  {footer_box} {{ content: {css_string(footer)}; white-space: pre-wrap; font-size: {t.caption - 0.5}pt;
                 color: {t.muted}; width: 150mm; vertical-align: top; padding-top: 3mm;
                 border-top: 0.5pt solid {t.line}; }}
  {page_no}
}}
@page cover {{ margin: 0; @top-left {{ content: none; }} @top-right {{ content: none; }}
  @bottom-left {{ content: none; }} @bottom-center {{ content: none; }} @bottom-right {{ content: none; }} }}
* {{ box-sizing: border-box; }}
html {{ font-family: "{t.font_pdf}", sans-serif; font-size: {t.body}pt; color: {t.ink}; }}
body {{ margin: 0; background: {t.background}; line-height: 1.55; }}
.num {{ font-variant-numeric: tabular-nums; }}
.cover {{ page: cover; width: 210mm; height: 297mm; position: relative; overflow: hidden; break-after: page; }}
.cover-band {{ background: {t.primary}; color: {fg}; }}
.cover-split .panel {{ position: absolute; left: 0; top: 0; right: 0; height: 58%; background: {t.primary}; color: {fg}; }}
.cover-minimal {{ background: {t.background}; border-top: 3mm solid {t.primary}; }}
.cover .deco {{ position: absolute; right: -10mm; top: 70mm; width: 150mm; opacity: 0.9; }}
.cover h1 {{ position: absolute; left: 20mm; top: 95mm; width: 130mm; margin: 0; font-size: {t.title + 10}pt; line-height: 1.25; }}
.cover .sub {{ position: absolute; left: 20mm; top: 150mm; width: 150mm; font-size: {t.heading}pt; opacity: 0.85; }}
.cover-minimal h1 {{ color: {t.ink}; }}
.cover-minimal .rule {{ position: absolute; left: 20mm; top: 142mm; width: 18mm; height: 1.2mm; background: {t.accent}; }}
.cover .meta {{ position: absolute; left: 20mm; right: 20mm; bottom: 26mm; display: grid;
  grid-template-columns: 1fr 1fr 1fr; gap: 5mm 8mm; border-top: 0.5pt solid currentColor; padding-top: 5mm; }}
.cover-split .meta, .cover-minimal .meta {{ color: {t.ink}; }}
.cover .meta .k {{ font-size: {t.caption}pt; opacity: 0.75; }}
.cover .meta .v {{ font-weight: 700; }}
.brand {{ position: absolute; display: flex; align-items: center; gap: 4mm; font-weight: 700; }}
.brand img {{ max-height: 14mm; max-width: 38mm; }}
.brand.tl {{ left: 20mm; top: 18mm; }} .brand.tr {{ right: 20mm; top: 18mm; }}
.brand.bl {{ left: 20mm; bottom: 12mm; }} .brand.br {{ right: 20mm; bottom: 12mm; }}
.brand.ct {{ left: 0; right: 0; top: 18mm; justify-content: center; }}
.cover-split .brand, .cover-band .brand {{ color: {fg}; }}
.cover-minimal .brand {{ color: {t.primary}; }}
.toc {{ break-after: page; }}
.toc h2 {{ font-size: {t.title}pt; }}
.toc a {{ display: block; color: {t.ink}; text-decoration: none; padding: 2mm 0; border-bottom: 0.5pt solid {t.line}; }}
.toc a::after {{ content: leader('.') target-counter(attr(href), page); color: {t.muted}; }}
.sec h2 {{ display: flex; align-items: center; gap: 3mm; font-size: {t.title}pt; margin: 10mm 0 4mm;
  padding-bottom: 2mm; border-bottom: 0.5pt solid {t.line}; break-after: avoid; }}
.sec h2 .ic {{ display: inline-block; background: {t.primary}; border-radius: 1.5mm; padding: 1.2mm; line-height: 0; }}
h3 {{ font-size: {t.heading}pt; margin: 5mm 0 2mm; break-after: avoid; border-left: 1mm solid {t.accent}; padding-left: 2.5mm; }}
p {{ margin: 0 0 3mm; }}
p.boxed {{ background: {t.pale}; padding: 3mm 4mm; border-radius: 1.5mm; break-inside: avoid; }}
ul, ol {{ margin: 0 0 3mm; padding-left: 6mm; }}
li {{ margin-bottom: 1.2mm; }} li::marker {{ color: {t.primary}; }}
ul.checks {{ list-style: none; padding-left: 5mm; }} ul.checks li::before {{ content: "✓ "; color: {t.primary}; }}
.grid {{ display: grid; gap: 3mm; margin-bottom: 4mm; }}
.grid.c1 {{ grid-template-columns: 1fr; }} .grid.c2 {{ grid-template-columns: 1fr 1fr; }}
.grid.c3 {{ grid-template-columns: 1fr 1fr 1fr; }} .grid.c4 {{ grid-template-columns: 1fr 1fr 1fr 1fr; }}
.card {{ border: 0.5pt solid {t.line}; border-top: 1mm solid {t.primary}; padding: 3mm; break-inside: avoid; background: {t.background}; }}
.card.warn {{ border-top-color: {t.out}; }}
.card .t {{ font-size: {t.caption}pt; color: {t.muted}; font-weight: 700; }}
.card .v {{ font-size: {t.title}pt; font-weight: 700; line-height: 1.2; }}
.card .v small {{ font-size: {t.caption}pt; color: {t.muted}; font-weight: 400; margin-left: 1mm; }}
.card .l {{ font-size: {t.small}pt; }}
.tag {{ font-size: {t.small}pt; font-weight: 700; }}
.tone-within {{ color: {t.within}; }} .tone-out {{ color: {t.out}; }} .tone-alert {{ color: {t.alert}; }}
.tone-info {{ color: {t.primary}; }} .tone-neutral {{ color: {t.muted}; }}
.bar {{ position: relative; height: 3mm; margin-top: 1.5mm; }}
.bar .track {{ position: absolute; left: 0; right: 0; top: 1mm; height: 1mm; background: {t.pale}; }}
.bar .range {{ position: absolute; top: 1mm; height: 1mm; background: {t.primary_soft}; }}
.bar .mark {{ position: absolute; top: 0; width: 0.6mm; height: 3mm; background: {t.ink}; }}
table {{ width: 100%; border-collapse: collapse; margin-bottom: 4mm; font-size: {t.small}pt; }}
thead {{ display: table-header-group; }}
th {{ background: {t.pale}; color: {t.primary}; text-align: left; }}
th, td {{ padding: 1.8mm 2.2mm; border-bottom: 0.5pt solid {t.line}; vertical-align: top; overflow-wrap: anywhere; }}
td.hl {{ color: {t.out}; font-weight: 700; }}
tr {{ break-inside: avoid; }}
dl.kv {{ display: grid; gap: 1mm 8mm; margin: 0 0 4mm; }}
dl.kv.c2 {{ grid-template-columns: 1fr 1fr; }} dl.kv.c1 {{ grid-template-columns: 1fr; }}
dl.kv div {{ display: grid; grid-template-columns: 34% 1fr; gap: 2mm; border-bottom: 0.5pt solid {t.line}; padding: 1.2mm 0; break-inside: avoid; }}
dl.kv dt {{ color: {t.muted}; font-size: {t.small}pt; }} dl.kv dd {{ margin: 0; }}
.callout {{ background: {t.pale}; border-left: 1.2mm solid {t.primary}; padding: 3mm 4mm; margin-bottom: 4mm; break-inside: avoid; }}
.callout.warn {{ background: {t.pale_out}; border-left-color: {t.out}; }}
.callout.alert {{ background: {t.pale_alert}; border-left-color: {t.alert}; }}
.callout .ct {{ font-weight: 700; }}
.callout.alert .ct {{ color: {t.alert}; }} .callout.warn .ct {{ color: {t.out}; }}
.chart {{ break-inside: avoid; margin-bottom: 4mm; }}
.donut {{ display: grid; grid-template-columns: 45mm 1fr; gap: 6mm; align-items: center; break-inside: avoid; margin-bottom: 4mm; }}
.donut .ring {{ position: relative; }}
.donut .center {{ position: absolute; left: 0; right: 0; top: 44%; text-align: center; font-weight: 700; }}
.donut .sw {{ display: inline-block; width: 3mm; height: 3mm; margin-right: 2mm; vertical-align: middle; }}
.timeline {{ display: grid; gap: 4mm; margin-bottom: 4mm; border-top: 0.5pt solid {t.line}; padding-top: 3mm; }}
.step {{ break-inside: avoid; }}
.step .dot {{ width: 3mm; height: 3mm; border-radius: 50%; background: {t.primary}; margin-top: -4.6mm; margin-bottom: 1.5mm; }}
.step .lbl {{ font-weight: 700; }}
.step .box {{ background: {t.pale}; padding: 2mm; border-radius: 1.5mm; font-size: {t.small}pt; margin-top: 1mm; }}
.cols {{ display: grid; gap: 3mm; margin-bottom: 4mm; }}
.col {{ background: {t.pale}; border-top: 1mm solid {t.primary}; padding: 3mm; break-inside: avoid; }}
.col .ch {{ font-weight: 700; font-size: {t.heading}pt; margin-bottom: 1.5mm; }}
.col.tone-within {{ border-top-color: {t.within}; }} .col.tone-out {{ border-top-color: {t.out}; }}
.col.tone-alert {{ border-top-color: {t.alert}; }}
.col ul {{ font-size: {t.small}pt; color: {t.ink}; }}
.media {{ display: grid; grid-template-columns: 62% 1fr; gap: 4mm; background: {t.pale}; padding: 3mm; break-inside: avoid; margin-bottom: 4mm; }}
.media .mn {{ color: {t.primary}; font-weight: 700; font-size: {t.heading}pt; }}
.media a {{ display: block; background: {t.background}; border: 0.5pt solid {t.line}; border-radius: 1.5mm;
  padding: 3mm; color: {t.primary}; font-weight: 700; text-decoration: none; }}
figure {{ margin: 0 0 4mm; break-inside: avoid; }} figure img {{ max-width: 100%; max-height: 110mm; }}
figcaption {{ font-size: {t.caption}pt; color: {t.muted}; }}
.sleep {{ display: grid; grid-template-columns: 14mm 1fr 32mm; gap: 3mm; align-items: center; margin-bottom: 2mm; }}
.sleep .lab {{ color: {t.muted}; font-weight: 700; }}
"""


def _cover(content: dict, style: dict, t: Theme, base_dir: Path, warnings: list[str]) -> str:
    variant = style["cover.variant"]
    brand = content.get("brand") or {}
    fg = on_color(t.primary)
    parts = [f'<section class="cover cover-{variant}">']
    if variant == "split":
        parts.append('<div class="panel"></div>')
    if variant == "band":
        parts.append(f'<div class="deco">{cover_deco_svg(t, fg)}</div>')
    if variant == "minimal":
        parts.append('<div class="rule"></div>')
    logo_html = ""
    raw = brand.get("logo_path")
    if raw:
        p = Path(raw)
        p = p if p.is_absolute() else base_dir / p
        if p.is_file():
            logo_html = f'<img src="{e(p.resolve().as_uri())}" alt="">'
        else:
            warnings.append(f"LOGO 文件不存在或无法读取，封面只保留机构名称：{raw}")
    lp, op = style["brand.logo_position"], style["brand.org_position"]
    org = f'<span>{e(brand["org_name"])}</span>' if brand.get("org_name") else ""
    if lp == op:
        if logo_html or org:
            parts.append(f'<div class="brand {_POS_CLASS[lp]}">{logo_html}{org}</div>')
    else:
        if logo_html:
            parts.append(f'<div class="brand {_POS_CLASS[lp]}">{logo_html}</div>')
        if org:
            parts.append(f'<div class="brand {_POS_CLASS[op]}">{org}</div>')
    parts.append(f"<h1>{e(content['title'])}</h1>")
    if content.get("subtitle"):
        parts.append(f'<div class="sub">{e(content["subtitle"])}</div>')
    meta = [("客户", content["client"]["name"])]
    meta += [(f["label"], f["value"]) for f in content["client"].get("facts", [])]
    if content.get("data_basis"):
        meta.append(("数据依据", content["data_basis"]))
    meta.append(("生成日期", content["generated_at"]))
    if content.get("manager"):
        meta.append((content["manager"].get("title") or "负责人", content["manager"]["name"]))
    cells = "".join(f'<div><div class="k">{e(k)}</div><div class="v num">{e(v)}</div></div>' for k, v in meta)
    parts.append(f'<div class="meta">{cells}</div></section>')
    return "".join(parts)


def _card(c: Any) -> str:
    warn = " warn" if c.tag and c.tag.tone in ("out", "alert") else ""
    out = [f'<div class="card{warn}">']
    title = f"{c.badge}  {c.title}".strip() if c.badge else c.title
    if title:
        out.append(f'<div class="t">{e(title)}</div>')
    if c.value or c.unit:
        unit = f"<small>{e(c.unit)}</small>" if c.unit else ""
        out.append(f'<div class="v num">{e(c.value)}{unit}</div>')
    out += [f'<div class="l">{e(ln)}</div>' for ln in c.lines]
    if c.tag:
        out.append(f'<div class="tag tone-{c.tag.tone}">{e(c.tag.text)}</div>')
    if c.bar:
        span = c.bar.high - c.bar.low or 1.0
        dmin, dmax = c.bar.low - 0.6 * span, c.bar.high + 0.6 * span
        pos = (min(max(c.bar.value, dmin), dmax) - dmin) / (dmax - dmin) * 100
        left = (c.bar.low - dmin) / (dmax - dmin) * 100
        width = span / (dmax - dmin) * 100
        out.append(f'<div class="bar"><div class="track"></div><div class="range" style="left:{left:.1f}%;width:{width:.1f}%">'
                   f'</div><div class="mark" style="left:{pos:.1f}%"></div></div>')
    out.append("</div>")
    return "".join(out)


def _prim(p: Prim, t: Theme, base_dir: Path) -> str:
    if isinstance(p, SubHeading):
        return f"<h3>{e(p.text)}</h3>"
    if isinstance(p, Paragraph):
        cls = ' class="boxed"' if p.boxed else ""
        return f"<p{cls}>{e(p.text).replace(chr(10), '<br>')}</p>"
    if isinstance(p, Bullets):
        tag = "ol" if p.style == "numbers" else "ul"
        cls = ' class="checks"' if p.style == "checks" else ""
        return f"<{tag}{cls}>" + "".join(f"<li>{e(i)}</li>" for i in p.items) + f"</{tag}>"
    if isinstance(p, CardGrid):
        return f'<div class="grid c{p.cols}">' + "".join(_card(c) for c in p.cards) + "</div>"
    if isinstance(p, Table):
        head = "".join(f"<th>{e(c)}</th>" for c in p.columns)
        rows = "".join(
            "<tr>" + "".join(
                f'<td class="{"hl" if i == p.highlight_col and v not in ("", "—") else ""}">{e(v)}</td>'
                for i, v in enumerate(r)) + "</tr>"
            for r in p.rows
        )
        return f"<table><thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table>"
    if isinstance(p, KeyValue):
        return f'<dl class="kv c{p.cols}">' + "".join(
            f"<div><dt>{e(k)}</dt><dd>{e(v)}</dd></div>" for k, v in p.pairs) + "</dl>"
    if isinstance(p, Callout):
        title = f'<div class="ct">{e(p.title)}</div>' if p.title else ""
        return f'<div class="callout {p.tone}">{title}<div>{e(p.text).replace(chr(10), "<br>")}</div></div>'
    if isinstance(p, Chart):
        if p.kind == "donut":
            colors = donut_colors(t)
            legend = "".join(f'<div><span class="sw" style="background:{colors[i % len(colors)]}"></span>{e(x)}</div>'
                             for i, x in enumerate(p.legend))
            center = f'<div class="center">{e(p.center)}</div>' if p.center else ""
            return f'<div class="donut"><div class="ring">{donut_svg(p, t)}{center}</div><div>{legend}</div></div>'
        return f'<div class="chart">{line_svg(p, t) if p.kind == "line" else bar_svg(p, t)}</div>'
    if isinstance(p, Timeline):
        per = min(5, len(p.steps))
        steps = "".join(
            f'<div class="step"><div class="dot"></div><div class="lbl">{e(s.label)}</div>'
            + (f'<div class="box">{"<br>".join(e(x) for x in s.lines)}</div>' if s.lines else "") + "</div>"
            for s in p.steps)
        return f'<div class="timeline" style="grid-template-columns: repeat({per}, 1fr)">{steps}</div>'
    if isinstance(p, Columns):
        per = len(p.columns) if len(p.columns) <= 4 else 3
        cols = "".join(
            f'<div class="col tone-{c.tone}"><div class="ch">{e(c.title)}</div><ul>'
            + "".join(f"<li>{e(i)}</li>" for i in c.items) + "</ul></div>"
            for c in p.columns)
        return f'<div class="cols" style="grid-template-columns: repeat({per}, 1fr)">{cols}</div>'
    if isinstance(p, Media):
        return (f'<div class="media"><div><div class="mn">{e(p.name)}</div><div>{e(p.description)}</div></div>'
                f'<a href="{e(p.url)}">查看示范/产品详情：{e(p.name)}</a></div>')
    if isinstance(p, Image):
        path = Path(p.path)
        path = path if path.is_absolute() else base_dir / path
        cap = f"<figcaption>{e(p.caption)}</figcaption>" if p.caption else ""
        return f'<figure><img src="{e(path.resolve().as_uri())}" alt="">{cap}</figure>'
    if isinstance(p, TimeBars):
        rows = []
        for i, (label, bed, wake) in enumerate(p.rows):
            color = t.primary if i == len(p.rows) - 1 else t.primary_soft
            rows.append(f'<div class="sleep"><div class="lab">{e(label)}</div><div>{timebar_svg(bed, wake, color, t)}</div>'
                        f'<div class="num">{e(bed)} – {e(wake)}</div></div>')
        return "".join(rows)
    raise TypeError(type(p).__name__)


def build_html(content: dict, style: dict, theme: Theme, base_dir: Path) -> tuple[str, list[str]]:
    warnings: list[str] = []
    t = theme
    parts = ['<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">',
             f"<style>{_css(content, style, t)}</style></head><body>",
             _cover(content, style, t, base_dir, warnings)]
    if style["toc"] == "on" or (style["toc"] == "auto" and len(content["sections"]) >= 6):
        links = "".join(f'<a href="#sec-{e(s["id"])}">{i:02d}　{e(s["title"])}</a>'
                        for i, s in enumerate(content["sections"], start=1))
        parts.append(f'<section class="toc"><h2>目录</h2>{links}</section>')
    fg = on_color(t.primary)
    for sec, items in section_prims(content, style):
        icon = f'<span class="ic">{icon_svg(icon_for_section(sec), fg, 4.5)}</span>' if style["section.icons"] else ""
        body = "".join(_prim(p, t, base_dir) for p, _ in items)
        parts.append(f'<section class="sec" id="sec-{e(sec["id"])}"><h2>{icon}{e(sec["title"])}</h2>{body}</section>')
    parts.append("</body></html>")
    return "".join(parts), warnings


def render_pdf(content: dict, style: dict, out_path: Path, base_dir: Path) -> list[str]:
    try:
        from weasyprint import HTML
    except ImportError as exc:
        raise RuntimeError("当前环境缺少 weasyprint，无法生成 PDF（沙箱镜像已预装）") from exc
    from hpr.theme import build_theme

    html, warnings = build_html(content, style, build_theme(style, "pdf"), base_dir)
    HTML(string=html, base_url=str(base_dir)).write_pdf(str(out_path))
    return warnings

```

- [ ] **Step 5: `qa.py` 加 PDF 部分**

在 `qa.py` 末尾追加：

```python
PAGE_NO_RE = re.compile(r"第\d+页/共\d+页")


def pdf_text(path: Path) -> str:
    from pypdf import PdfReader

    return "\n".join(page.extract_text() or "" for page in PdfReader(str(path)).pages)


def pdf_fonts(path: Path) -> list[str]:
    from pypdf import PdfReader

    names: set[str] = set()
    for page in PdfReader(str(path)).pages:
        fonts = (page.get("/Resources") or {}).get("/Font") or {}
        for ref in fonts.values():
            names.add(str(ref.get_object().get("/BaseFont", "")))
    return sorted(names)


def _pdf_chrome(content: dict) -> list[str]:
    brand = content.get("brand") or {}
    line1 = "  ｜  ".join(v for v in (brand.get("org_name"), brand.get("footer_signature")) if v)
    return [norm(x) for x in (content["title"], brand.get("org_name", ""), line1, brand.get("disclaimer", "")) if x]


def qa_pdf(path: Path, content: dict, required: list[str], theme: Theme) -> dict[str, Any]:
    from pypdf import PdfReader

    raw = norm(pdf_text(path))
    cleaned = PAGE_NO_RE.sub("", raw)
    for chrome in sorted(_pdf_chrome(content), key=len, reverse=True):
        cleaned = cleaned.replace(chrome, "")
    missing = [r for r in dict.fromkeys(required) if norm(r) not in raw and norm(r) not in cleaned]
    fonts = pdf_fonts(path)
    embedded = any("NotoSansCJK" in f.replace(" ", "") for f in fonts)
    cr = contrast_report(theme)
    passed = not missing and embedded and cr["ok"]
    return {
        "status": "passed" if passed else "failed",
        "pages": len(PdfReader(str(path)).pages),
        "missing": missing,
        "overflow": [],
        "out_of_bounds": [],
        "fonts_embedded": embedded,
        "contrast": cr,
    }
```

（PDF 由 weasyprint 流式排版，不会产生框内溢出；「越界」同理为空。）

- [ ] **Step 6: `render.py` 接入 PDF**

删除这两行：

```python
    if "pdf" in formats:
        return _fail(["PDF 输出在本版本尚未提供，请先用 --format pptx"])
```

把「渲染 + 质检」段替换为：

```python
    m = Measurer()
    required = required_texts(content)
    files: dict[str, str] = {}
    qa: dict[str, dict] = {}
    warnings: list[str] = []
    try:
        if "pptx" in targets:
            warnings += render_pptx(content, style, targets["pptx"], base_dir, m)
            qa["pptx"] = qa_pptx(targets["pptx"], required, build_theme(style, "pptx"), m)
            files["pptx"] = str(targets["pptx"])
        if "pdf" in targets:
            warnings += render_pdf(content, style, targets["pdf"], base_dir)
            qa["pdf"] = qa_pdf(targets["pdf"], content, required, build_theme(style, "pdf"))
            files["pdf"] = str(targets["pdf"])
    except (RenderError, LayoutError, RuntimeError) as exc:
        return _fail([str(exc)])
```

并在文件头导入 `from hpr.pdf_html import render_pdf`、`from hpr.qa import qa_pdf, qa_pptx`，模块文档字符串改为 `"""Render a health-plan content JSON to PPTX and/or PDF with hard QA gates."""`。警告去重：`warnings = list(dict.fromkeys(warnings))`（两种格式各报一次同一 LOGO 缺失）。

- [ ] **Step 7: 写镜像内用例 `platform-skills/tests/in_image/case_hpr_render.py`**

```python
import json
import os
import shutil
import sys
from pathlib import Path

from _harness import check, run, script
from PIL import Image

os.chdir("/workspace")
skill = Path(os.environ["EXPERT_WORK_SKILLS_DIR"]) / "health-plan-report"
shutil.copy(skill / "sample" / "sample-plan.json", "plan.json")
Image.new("RGB", (640, 360), "#DDEEEE").save("trend-note.png")

out = json.loads(
    run(["python", script("health-plan-report", "render.py"), "--content", "plan.json", "--format", "both",
         "--out-dir", "out", "--basename", "样例_20260928100000"], timeout=600).stdout
)
check(out["ok"] is True, f"render not ok: {out}")
check(out["qa"]["pptx"]["status"] == "passed", f"pptx qa: {out['qa']['pptx']}")
check(out["qa"]["pdf"]["status"] == "passed", f"pdf qa: {out['qa']['pdf']}")
check(out["qa"]["pdf"]["fonts_embedded"], "CJK font not embedded in PDF")
check(out["qa"]["pdf"]["pages"] >= 4, f"too few PDF pages: {out['qa']['pdf']['pages']}")

sys.path.insert(0, str(skill / "scripts"))
from hpr.measure import Measurer  # noqa: E402

check(Measurer().real, "real CJK font not found in image — measurement would be approximate")

pdf = json.loads(
    run(["python", script("health-plan-report", "render.py"), "--content", "plan.json", "--format", "pptx",
         "--out-dir", "out", "--basename", "样例_20260928100000"], expect=1).stdout
)
check(any("已存在" in e for e in pdf["errors"]), "existing output must be refused")

run(["soffice", "--headless", "--convert-to", "pdf", "--outdir", "lo", "out/样例_20260928100000.pptx"], timeout=300)
check(Path("lo/样例_20260928100000.pdf").is_file(), "LibreOffice could not open the PPTX")
print("PASS case_hpr_render")
```

（`from hpr.measure import Measurer` 必须在 `sys.path.insert` 之后，故保留 `# noqa: E402`。）

- [ ] **Step 8: 运行本地测试 + 镜像测试**

```bash
uv run --no-sync pytest platform-skills/tests -q
export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock
export EXPERT_WORK_SKILLS_TEST_IMAGE=crpi-sgadimluo7wm655m.cn-hangzhou.personal.cr.aliyuncs.com/expert-work/sandbox:7ac31957
uv run --no-sync pytest platform-skills/tests/test_in_image.py -q -k hpr
```

Expected: 本地全绿；镜像用例 `case_hpr_render.py` PASS。镜像里若 `qa.pdf.missing` 非空：先看缺的是哪类文字——若是跨页段落，检查 `_pdf_chrome` 是否把页眉页脚全部剔除；若是表格单元格，检查 `overflow-wrap: anywhere` 是否生效。不得放宽比对规则。

break → red → restore → green：
- `pdf_html._prim` 的 `Paragraph` 分支改成输出空 `<p></p>` → `test_every_required_text_is_in_visible_html` 红；还原。
- `qa_pdf` 里 `embedded` 恒为 `True` 的变异无法在本地验证——在镜像里把 `@font-face`/`font-family` 改成不存在的字体名跑一次，`fonts_embedded` 应为 `False`、用例红；还原。

- [ ] **Step 9: lint 与提交**

```bash
uv run ruff format platform-skills/health-plan-report platform-skills/tests
uv run ruff check platform-skills && uv run ruff format --check platform-skills
git add platform-skills/health-plan-report/scripts platform-skills/tests
git commit -m "feat(health-plan-report): A4 PDF output via weasyprint with QA and in-image test"
```

---

### Task 8: 全组合矩阵 + 异常输入

**Files:**
- Create: `platform-skills/tests/health_plan_report/test_matrix.py`

**Interfaces:**
- Consumes: `hpr.ppt_draw.render_pptx`、`hpr.qa.qa_pptx`、`hpr.pdf_html.build_html`、`hpr.qa.missing_texts`、`hpr.style.Layer / resolve`、`hpr.catalog.VARIANTS`

- [ ] **Step 1: 写测试**

```python
import itertools

import pytest
from hpr.catalog import VARIANTS
from hpr.content import required_texts
from hpr.measure import Measurer
from hpr.pdf_html import build_html
from hpr.ppt_draw import render_pptx
from hpr.ppt_layout import LayoutError
from hpr.qa import missing_texts, qa_pptx
from hpr.style import Layer, resolve
from hpr.theme import build_theme
from PIL import Image as PILImage

M = Measurer("/nonexistent")
COLORS = ["#0B4F5C", "浅蓝", "#111111", "墨绿"]
SCALES = ["compact", "standard", "large"]
DENSITY = ["compact", "standard", "airy"]
COVERS = ["band", "split", "minimal"]


@pytest.fixture
def base(tmp_path):
    PILImage.new("RGB", (640, 360), "#DDEEEE").save(tmp_path / "trend-note.png")
    return tmp_path


def _check(content, base, values, name="m.pptx"):
    style = resolve([Layer("x", values)], content).style
    out = base / name
    if out.exists():
        out.unlink()
    render_pptx(content, style, out, base, M)
    qa = qa_pptx(out, required_texts(content), build_theme(style, "pptx"), M)
    assert qa["status"] == "passed", (values, qa)


@pytest.mark.parametrize(("color", "scale", "density", "cover"), list(itertools.product(COLORS, SCALES, DENSITY, COVERS)))
def test_style_matrix_pptx(sample, base, color, scale, density, cover):
    _check(sample, base, {"color.primary": color, "type.scale": scale, "layout.density": density,
                          "cover.variant": cover, "toc": "on"})


@pytest.mark.parametrize(("kind", "variant"), [(k, v) for k, vs in VARIANTS.items() for v in vs])
def test_every_block_variant_pptx(sample, base, kind, variant):
    _check(sample, base, {f"blocks.{kind}.variant": variant})


@pytest.mark.parametrize(("kind", "variant"), [(k, v) for k, vs in VARIANTS.items() for v in vs])
def test_every_block_variant_html_keeps_text(sample, base, kind, variant):
    style = resolve([Layer("x", {f"blocks.{kind}.variant": variant})], sample).style
    html, _ = build_html(sample, style, build_theme(style, "pdf"), base)
    import html as h
    import re

    visible = h.unescape(re.sub(r"<style>.*?</style>|<svg.*?</svg>|<[^>]+>", "", html, flags=re.S))
    assert missing_texts(required_texts(sample), visible) == []


def test_hundred_row_table_spans_pages(sample, base):
    tbl = next(b for s in sample["sections"] for b in s["blocks"] if b["kind"] == "table")
    tbl["rows"] = [[f"第 {i} 天", "—", "—", "—"] for i in range(1, 101)]
    _check(sample, base, {})


def test_long_texts_everywhere(sample, base):
    long = "这是一段用于压力测试的较长说明文字，包含数字 123 和英文 words。" * 6
    for sec in sample["sections"]:
        for b in sec["blocks"]:
            if b["kind"] == "paragraph":
                b["text"] = long * 5
            if b["kind"] == "issues":
                for it in b["items"]:
                    it["evidence"] = long
    _check(sample, base, {"type.scale": "large"})


def test_one_section_one_paragraph(base):
    content = {"schema_version": "1", "title": "李先生健康管理方案", "generated_at": "2026-09-28",
               "client": {"name": "李先生"}, "sections": [{"id": "s", "title": "说明", "blocks": [{"kind": "paragraph", "text": "一句话。"}]}]}
    _check(content, base, {})


def test_bad_logo_bytes_warns(sample, base):
    (base / "bad.png").write_bytes(b"not a png")
    sample["brand"]["logo_path"] = "bad.png"
    style = resolve([], sample).style
    warnings = render_pptx(sample, style, base / "w.pptx", base, M)
    assert any("LOGO" in w for w in warnings)


def test_too_many_cover_facts_is_error(sample, base):
    sample["client"]["facts"] = [{"label": f"项{i}", "value": "值"} for i in range(14)]
    with pytest.raises(LayoutError, match="client.facts"):
        render_pptx(sample, resolve([], sample).style, base / "f.pptx", base, M)


def test_unknown_variant_name_is_reported_not_fatal(sample, base):
    res = resolve([Layer("x", {"blocks.trend.variant": "雷达图"})], sample)
    assert next(e for e in res.report if e["key"] == "blocks.trend.variant")["status"] == "out_of_range"
    _check(sample, base, {"blocks.trend.variant": "雷达图"})
```

（矩阵共 4×3×3×3 = 108 组 + 各积木版式 38 组；本地耗时应在 2 分钟内。若超过 5 分钟，把 `COLORS` 缩到 `["#0B4F5C", "浅蓝"]` 并在 ledger 记一条裁定，不要删维度。）

- [ ] **Step 2: 运行**

Run: `uv run --no-sync pytest platform-skills/tests/health_plan_report/test_matrix.py -q`
Expected: 全部 PASS。任何一组红：修绘制/版式，不改测试。

- [ ] **Step 3: 提交**

```bash
uv run ruff format platform-skills/tests/health_plan_report && uv run ruff check platform-skills
git add platform-skills/tests/health_plan_report/test_matrix.py platform-skills/health-plan-report/scripts
git commit -m "test(health-plan-report): style × variant matrix and abnormal inputs"
```

---
### Task 9: SKILL.md 正文 + 参考文档 + 包级检查

**Files:**
- Modify: `platform-skills/health-plan-report/SKILL.md`（整体重写）
- Create: `platform-skills/health-plan-report/reference/content-schema.md`
- Create: `platform-skills/health-plan-report/reference/style-options.md`
- Create: `platform-skills/health-plan-report/reference/migration.md`
- Create: `platform-skills/tests/health_plan_report/test_skill_package.py`

**Interfaces:**
- Consumes: T1–T8 全部 CLI 与键名（SKILL.md 中出现的每个键、每个命令都必须真实存在——测试会逐项核对）

- [ ] **Step 1: 写失败测试 `test_skill_package.py`**

```python
import re
import zipfile
from pathlib import Path

import pytest
import yaml
from control_plane.api._skill_moderation import (
    moderate_prompt_fragment,
    moderate_required_models,
    moderate_tool_names,
)
from control_plane.api._skill_zip import parse_skill_zip
from expert_work.common.threat_patterns import scan_for_threats
from hpr.catalog import VARIANTS
from hpr.style import OPTIONS

NAME = "health-plan-report"
REF = re.compile(r"\$EXPERT_WORK_SKILLS_DIR/health-plan-report/scripts/([A-Za-z0-9_]+\.py)")
KEY = re.compile(r"`((?:color|type|layout|brand|footer|cover|section|output)\.[a-z_.]+|toc)`")


@pytest.fixture(scope="module")
def pkg(built_packages) -> Path:
    return built_packages[NAME]


@pytest.fixture(scope="module")
def root(unpacked_skills) -> Path:
    return unpacked_skills / NAME


def _body(root: Path) -> tuple[dict, str]:
    _, fm, body = (root / "SKILL.md").read_text(encoding="utf-8").split("---\n", 2)
    return yaml.safe_load(fm), body


def test_platform_parse_and_moderation(pkg):
    payload = parse_skill_zip(pkg.read_bytes(), asset_tier=False)
    assert payload.name == NAME
    assert payload.lazy_load is True
    moderate_prompt_fragment(payload.prompt_fragment, lazy_load=payload.lazy_load)
    moderate_tool_names(payload.tool_names)
    moderate_required_models(payload.required_models)


def test_frontmatter(root):
    fm, _ = _body(root)
    assert fm["name"] == NAME
    assert 0 < len(fm["description"]) <= 200
    assert fm["license"] == "深护智康自研，仅限本平台使用"
    assert fm["expert_work"] == {"lazy": True, "category": "健康"}


def test_body_length(root):
    _, body = _body(root)
    assert len(body) <= 6000, len(body)


def test_scripts_referenced_both_ways(root):
    _, body = _body(root)
    referenced = set(REF.findall(body))
    present = {p.name for p in (root / "scripts").glob("*.py")} - {"_cli.py"}
    assert referenced == present


def test_reference_docs_linked_and_present(root):
    _, body = _body(root)
    for name in ("content-schema.md", "style-options.md", "migration.md"):
        assert f"reference/{name}" in body
        assert (root / "reference" / name).is_file()


def test_every_style_key_in_docs_exists(root):
    text = (root / "SKILL.md").read_text(encoding="utf-8") + (root / "reference" / "style-options.md").read_text(encoding="utf-8")
    for key in set(KEY.findall(text)):
        assert key in OPTIONS or key.startswith("blocks.") or key.startswith("sections.") or key.startswith("brand."), key
    for key in OPTIONS:
        assert f"`{key}`" in text, f"style-options.md 漏写 {key}"


def test_every_kind_and_variant_documented(root):
    text = (root / "reference" / "content-schema.md").read_text(encoding="utf-8")
    for kind, variants in VARIANTS.items():
        assert f"`{kind}`" in text, kind
        for v in variants:
            assert f"`{v}`" in text, (kind, v)


def test_check_section_uses_read_page_then_ask_image(root):
    _, body = _body(root)
    m = re.search(r"^## 检查成品\n(.*?)(?=^## )", body, re.S | re.M)
    assert m
    sec = m.group(1)
    assert 0 <= sec.find("read_page(") < sec.find("ask_image(")
    for call in re.findall(r"ask_image\(([^)]*)\)", sec):
        assert ".png" not in call.lower()


def test_no_tenant_or_agent_names_anywhere(root):
    banned = ("ai-health-plan", "ahp", "张女士", "苹果夹子")
    for f in root.rglob("*"):
        if f.is_file():
            text = f.read_text(encoding="utf-8")
            for word in banned:
                assert word not in text, (f, word)
            lines = [ln for ln in text.splitlines() if "深护" in ln]
            assert all(ln.startswith("license:") for ln in lines), (f, lines)


def test_threat_scans(root):
    for f in sorted(root.rglob("*")):
        if f.is_file():
            text = f.read_text(encoding="utf-8")
            assert not scan_for_threats(text, scope="strict"), f
            assert not scan_for_threats(text, scope="context"), f


def test_package_is_text_only(pkg):
    with zipfile.ZipFile(pkg) as z:
        for name in z.namelist():
            z.read(name).decode("utf-8")
```

注：`built_packages` / `unpacked_skills` 夹具定义在 `platform-skills/tests/conftest.py`，对子目录测试同样可见。

- [ ] **Step 2: 运行，确认失败**

Run: `uv run --no-sync pytest platform-skills/tests/health_plan_report/test_skill_package.py -q`
Expected: 多条 FAIL（正文仍是 T1 占位，reference 不存在）。

- [ ] **Step 3: 重写 `SKILL.md`**

```markdown
---
name: health-plan-report
description: 把已定稿的健康管理方案内容渲染成企业级的可编辑 PPT（16:9）与 A4 PDF，并做内容与版式质检。只负责呈现：取数、健康判断与方案内容由你（调用方）决定。
license: 深护智康自研，仅限本平台使用
expert_work:
  lazy: true
  category: 健康
---

健康方案定稿后，用本技能出客户交付件（PPT / PDF）。版式、配色、字号、分页都由本技能按统一设计规范完成，
**不要自己写排版代码，也不要修改本技能的脚本**。

## 边界

- 本技能只呈现：你给的每个字原样出现在成品里，章节与内容顺序照你给的来；它不补写、不删减、不重排、不做健康判断。
- 取数、健康判断、红线、措辞、板块取舍都由你按自己的规则决定，写进内容 JSON。
- 超出可调范围的视觉要求（如加背景图、改成其它字体）如实告诉用户做不到。

## 环境

- 依赖已预装（python-pptx、weasyprint、Pillow、pypdf、中文字体 Noto Sans CJK），不要安装任何包。
- 工作目录 /workspace；脚本在 $EXPERT_WORK_SKILLS_DIR/health-plan-report/scripts/。
- 成品必须用 save_artifact 登记，否则用户拿不到。

## 流程

1. 按 `reference/content-schema.md` 写内容 JSON（例：`{FN}.json`），校验：
   `python $EXPERT_WORK_SKILLS_DIR/health-plan-report/scripts/validate.py {FN}.json`
   报错会给出位置（如 `sections[3].blocks[1].rows[4]`），改内容后重跑。
2. 准备样式层（可以没有）：把每一类参数各写成一个 JSON 小文件，**按优先级从高到低**传给渲染命令。
   常见顺序：本次对话要求 > 系统注入的样式配置 > 个人默认 `report-style/personal.json`（存在才传）。
3. 渲染：
   `python $EXPERT_WORK_SKILLS_DIR/health-plan-report/scripts/render.py --content {FN}.json --style 本次.json --style 配置.json --style report-style/personal.json --format pptx --out-dir . --basename {FN}`
   `--format` 取 `pptx` / `pdf` / `both`。同名文件已存在会拒绝覆盖——改版时换新的 basename。
4. 读输出 JSON：`ok` 为 false 时看 `errors` 或 `qa`（缺字、溢出、越界），按提示改内容或参数后重跑。
5. 按下面「检查成品」看图；没问题再 save_artifact（kind=document）。
6. 回复用户时，把输出里的 `not_applied`（没生效的参数及原因）与 `warnings`（LOGO 缺失、视频改链接等）如实告知。

## 内容 JSON

顶层写方案名称、生成日期、客户、可选的负责人与机构品牌（`brand`：机构名称、LOGO 路径、页脚署名、免责声明——
这些只从内容读取，样式改不了）。正文是有序章节，每章由积木组成：健康画像、核心问题、指标趋势、阶段目标、
阶段计划、营养处方、餐单、饮食原则、运动处方、睡眠、压力与情绪、生活习惯、动作/产品素材、监测计划、
就医提醒、采购清单、随访安排、方案摘要，以及段落、要点、表格、键值、图片、提示框。字段见
`reference/content-schema.md`，完整虚构样例在 `sample/sample-plan.json`。

## 样式与参数

- 可调项与取值见 `reference/style-options.md`：主色 `color.primary`、背景 `color.background`、字号档
  `type.scale`、密度 `layout.density`、LOGO 位置 `brand.logo_position`、封面样式 `cover.variant`、
  各积木版式（如 `blocks.meal_plan.variant`、`sections.diet.variant`）等。
- 值可以写大白话（「深蓝」「右上角」「大一点」「表格」），脚本负责换算；换算不了会出现在 `not_applied`。
- 你拿到的任何参数（注入变量、员工设置文本、对话要求）都由你翻译成这些键；影响内容的（增删板块、改周期）改内容 JSON。
- 用户说「以后都这样」才写个人默认：
  `python $EXPERT_WORK_SKILLS_DIR/health-plan-report/scripts/resolve_style.py --merge-into report-style/personal.json --set 键=值`
  一次性要求只放进本次样式层，不写文件。
- 想先看某组参数的生效结果：`python $EXPERT_WORK_SKILLS_DIR/health-plan-report/scripts/resolve_style.py --style a.json --content {FN}.json`

## 检查成品

1. 渲染输出 `ok: true` 表示机器质检通过（字全在、不溢出、不越界、对比度够）。
2. 再看图：`read_page(path={FN}.pptx, units=[1, N])` 渲染封面和内容最密的一页（一次最多 3 页），然后
   `ask_image(path={FN}.pptx, unit=同一页号, question=...)` 看中文是否正常、有无重叠、是否美观。
   必须先 read_page 再 ask_image。Agent 没有看图能力时跳过，并在回复里写明「未做视觉检查」。
3. 发现问题只调样式参数或内容后重新渲染（用新的 basename），不要改脚本。

## 用户要改

- 只改视觉（颜色、字号、某页版式、LOGO 位置）：同一份内容 JSON + 新的本次样式层，重新渲染。
- 改内容（换食谱、改目标、增删板块）：改内容 JSON 再渲染。
- 旧版本方案的视觉偏好迁移见 `reference/migration.md`。

## 做不到

背景图片、自定义字体、动画、二维码、HTML/H5 成品、改变渲染代码。PPT 用微软雅黑，客户电脑没有时 Office
会自动替换；需要绝对一致时建议交付 PDF（字体嵌入文件）。
```

写完运行 `python -c "import sys; b=open('platform-skills/health-plan-report/SKILL.md',encoding='utf-8').read().split('---\n',2)[2]; print(len(b))"`，确认 ≤ 6000；超出则压缩「内容 JSON」一节的积木列举。

- [ ] **Step 4: 写 `reference/content-schema.md`**

内容要求（逐项写全，测试会检查每个 kind 与 variant 名都以反引号出现）：

1. 顶层字段表：`schema_version`（固定 `"1"`）、`title`、`subtitle`、`period{label, weeks}`、`generated_at`（YYYY-MM-DD）、`data_basis`、`client{name, facts[{label, value}]}`、`manager{name, title}`、`brand{org_name, logo_path, footer_signature, disclaimer}`、`sections[]`——每行写「必填/可选、类型、说明」。
2. 章节：`{id, title, blocks[]}`；`id` 规则 `^[a-z][a-z0-9_-]{0,31}$`、文档内唯一；积木 `id` 可选、章节内唯一。
3. 每种积木一小节：标题写 `` `kind` 中文名 ``，列字段（与 `hpr/content.py` 的 `KIND_SCHEMAS` 完全一致：字段名、必填、类型）、一行 JSON 示例、可选版式（`` `cards` / `table` `` 这种写法，首个为默认），以及「呈现说明」一句。24 种：`summary` `profile` `issues` `trend` `goals` `phases` `nutrition` `meal_plan` `diet_rules` `exercise` `sleep` `stress` `habits` `material` `monitoring` `referral` `shopping` `follow_up` `paragraph` `bullets` `table` `kv` `image` `callout`。
4. 特别说明（照抄）：
   - 「渲染器只呈现：状态（`position`：`within` / `above` / `below` / `none`）、参考范围、目标值、阈值都由你给出，渲染器按你给的显示为「在参考范围内 / 高于参考范围 / 低于参考范围」，不做判断。」
   - 「`trend` 的 `points[].value`、`nutrition` 环形图的 `percent` 必须是数字；做不到时改用表格版式（`blocks.nutrition.variant` = `table`）。」
   - 「`image.path`、`material.media_path`、`brand.logo_path` 是相对内容 JSON 所在目录的路径；图片缺失会报错，LOGO / 视频缺失只降级并警告。」
   - 「表格每行列数必须等于 `columns`；空单元格写 `—`。」

生成方式建议：用一段一次性 Python（不提交）遍历 `KIND_SCHEMAS` 生成字段表骨架，再人工补示例与说明，保证与代码一致。

- [ ] **Step 5: 写 `reference/style-options.md`**

1. 叠加规则：层由高到低；同键取最高层；某层值无效时该层该键不生效、落到下一层；品牌四项（`brand.org_name` / `brand.logo_path` / `brand.footer_signature` / `brand.disclaimer`）只从内容读取。
2. 可调项全表（`OPTIONS` 13 个键逐行：键、取值、默认、说明），外加版式键三种写法：`blocks.<kind>.variant`、`sections.<章节id>.variant`、`sections.<章节id>.<积木id或序号>.variant`，与优先级（积木精确 > 章节 > 类型 > 默认）。
3. 大白话对照表（与 `hpr/style.py` 的 `ENUM_SYN` / `COLOR_NAMES` / `UP_WORDS` / `DOWN_WORDS` / `VARIANT_SYNONYMS` 一致）：
   - 「字太小 / 大一点 / 适合老年人」→ `type.scale`：`大一点` 或 `large`
   - 「太挤了 / 留白多一点」→ `layout.density`：`宽松一点`
   - 「换成蓝色 / 用深蓝」→ `color.primary`：`深蓝`
   - 「背景浅一点 / 浅灰底」→ `color.background`：`浅灰`（背景只支持浅色，深色会被自动调浅）
   - 「LOGO 放右上」→ `brand.logo_position`：`右上角`
   - 「饮食那页改成表格」→ `sections.<饮食章节id>.variant`：`表格`
   - 「餐单都用时间轴」→ `blocks.meal_plan.variant`：`时间轴`
   - 「不要页码」→ `footer.page_number`：`关`
   - 「不要目录」→ `toc`：`不要`
   - 「封面简洁一点」→ `cover.variant`：`极简`
   - 「只要 PDF / 两个都要」→ `output.formats`：`pdf` / `都要`
4. 参数落实清单的五种状态与向用户说明的话术（`applied` 不必说；`adjusted`、`out_of_range`、`brand_locked` 必须说；`overridden` 只在用户问起时说）。
5. 个人默认：文件 `report-style/personal.json`，只存用户明确要求长期生效的键，用 `resolve_style.py --merge-into ... --set` / `--unset` 维护，文件里保存用户原话值。

- [ ] **Step 6: 写 `reference/migration.md`**

面向「之前自己写过排版脚本、有旧风格记录」的调用方：
1. 旧的排版脚本（例如工作区里的 `style/*.py`）不再使用、不要删除、不要再执行。
2. 旧风格记录文件里：**视觉类偏好**（主色、背景、LOGO 位置、页脚对齐、某板块用表格还是卡片、图表还是表格）逐条翻译成 `resolve_style.py --merge-into report-style/personal.json --set 键=值`；**内容类偏好**（板块构成与顺序、放哪些数据）留在原记录里，继续由你自己在写内容 JSON 时遵守。
3. 翻译不了的视觉偏好（如指定某种字体）告诉用户当前不支持。
4. 迁移只做一次；之后以 `report-style/personal.json` 为准。

- [ ] **Step 7: 运行全部测试**

Run: `uv run --no-sync pytest platform-skills/tests -q`
Expected: 全部 PASS。

break → red → restore → green：
- 在 `SKILL.md` 的「检查成品」里把 `ask_image(` 挪到 `read_page(` 之前 → `test_check_section_uses_read_page_then_ask_image` 红；还原。
- 在 `reference/style-options.md` 删掉 `` `toc` `` 那行 → `test_every_style_key_in_docs_exists` 红；还原。

- [ ] **Step 8: lint 与提交**

```bash
uv run ruff check platform-skills && uv run ruff format --check platform-skills
git add platform-skills/health-plan-report platform-skills/tests/health_plan_report/test_skill_package.py
git commit -m "docs(health-plan-report): SKILL.md, content/style references, migration guide + package checks"
```

---

### Task 10: ai-health-plan 迁移 + 测试环境真栈验收（控制方执行；生产动作归用户）

本任务改的是 Agent 配置与测试环境数据，不改仓库代码（只改 ROADMAP）。每一步都在**测试环境**执行；生产发布另由用户按发布单操作。

- [ ] **Step 1: 构建并导入技能到测试环境（先 dry-run）**

```bash
uv run --no-sync python platform-skills/build.py --only health-plan-report
export KUBECONFIG=~/.kube/expert-work-test.yaml
POD=$(kubectl -n expert-work get pods -l app.kubernetes.io/name=control-plane --field-selector=status.phase=Running -o jsonpath='{.items[0].metadata.name}')
uv run --no-sync python platform-skills/import_in_pod.py bundle --dry-run platform-skills/dist/health-plan-report.skill | kubectl -n expert-work exec -i "$POD" -- python3 -
uv run --no-sync python platform-skills/import_in_pod.py bundle platform-skills/dist/health-plan-report.skill | kubectl -n expert-work exec -i "$POD" -- python3 -
```

Expected: dry-run 显示 create、零扫描命中；正式导入 `created`。

- [ ] **Step 2: 备份 ai-health-plan 当前配置（rev38）并起草 rev39 提示词**

用会话 scratchpad 里的 `ahp-opt/get_spec.py` 拉当前 spec 存为 `ahp-backup-rev38.json`。在 rev38 提示词文本上做以下**整段替换**（其余段落一字不动），存为 `prompt_rev39_draft.txt`：

1. 「第一步 → 5. 在用药」整条替换为：
   `5. 在用药:medication_query_plans(status=active)查当前用药;有用药时,饮食运动安排避开冲突(如降糖药下警惕空腹运动低血糖)。用药信息只作你内部安全判断的依据,成品里不出现任何用药字样。`
2. 「# 品牌版式(硬规则)」整节替换为：
   ```
   # 品牌版式(硬规则)
   - 机构名称(org_name)、LOGO(org_logo 下载到工作区后的文件路径)、页脚署名、免责声明(disclaimer 原文)写进内容 JSON 的 brand;为空就不写,不编造。
   - 方案名称 = 客户称呼 + 健康管理方案(如「张三健康管理方案」),写进内容 JSON 的 title。
   - 「LOGO 位置」有值时写进样式层 brand.logo_position(原话即可,如「右上角」);未设置时不写(默认左上角)。机构名称在封面的位置默认左上角,员工指定时写 brand.org_position。
   - LOGO 缺失或无法嵌入时不中断:按渲染输出的 warnings 告知员工。
   ```
3. 「# 默认视觉基线」整节**删除**（视觉由 health-plan-report 技能保证）。「方案背景颜色」一条改放进「员工样式」映射（见 4）。
4. 「# 素材使用(硬规则)」保留前两条与「任何情况都不承诺二维码」，把第 3、4 条替换为：
   `- 素材写成内容 JSON 的 material 积木:name、description(原话)、url;视频素材已下载到工作区时加 media_path。PPT 里能嵌入就嵌入、嵌不进自动改为可点击链接;PDF 一律是链接——这些由技能处理,你只需按 warnings 告知员工。`
5. 「# 风格一致性(按员工锚定)」整节替换为：
   ```
   # 风格一致性(按员工锚定)
   同一位员工每次生成的方案,内容结构与视觉风格都要前后一致。
   - 内容偏好锚:style/PLAN_STYLE.md,只记该员工的内容偏好——板块构成与顺序、哪些表单/字段的数据放进方案、数据用图还是表(写成版式偏好见下)。不记任何客户数据。组稿前先读它。
   - 视觉偏好锚:report-style/personal.json,由 health-plan-report 技能的 resolve_style.py --merge-into 维护,不要手写。
   - 首次使用本技能且 style/PLAN_STYLE.md 里有旧的视觉描述时,按技能 reference/migration.md 迁移一次;style/ 下旧的排版脚本不再使用,也不要删除。
   - 员工说「以后都…」:内容类写 PLAN_STYLE.md,视觉类用 --merge-into 写 personal.json;若与「我的设置」变量冲突,以设置为准并提醒员工去设置里改。
   - 员工说「这次…」:只放进本次样式层或本次内容,不写锚。
   - 除以上情况外不得修改 style/ 与 report-style/ 下文件。
   ```
6. 「# 复杂任务协作」中所有提到 `style/render_plan.py` 的地方改为 `health-plan-report 技能`；「两条禁令」改为「不得渲染或生成最终成品文件;不得修改 style/ 与 report-style/ 下任何文件」。
7. 「# 第三步:生成产物」的第 2、3、4 步替换为：
   ```
   2. 按 health-plan-report 技能的内容格式写方案 JSON 并登记:
      - 先读技能 reference/content-schema.md;每个板块选合适的积木(客户信息与目标→profile/goals/summary;周计划→phases;饮食→nutrition/meal_plan/diet_rules;运动→exercise+material;监测→monitoring;采购→shopping;注意事项→referral(有就医级信号时放最前)+bullets;员工自定义板块→paragraph/bullets/table)。
      - write_file(path="{FN}.json") → 用技能 validate.py 校验通过 → save_artifact(name="{FN}.json", path="{FN}.json", kind="data")。
   3. 组装样式层并渲染(这一步由主助手执行,不下放给子智能体):
      - 本次对话里的视觉要求 → 写 {FN}.style-now.json;「员工方案样式配置」里的视觉项(LOGO 位置、方案背景颜色→color.background、字号、版式等)→ 写 {FN}.style-conf.json;report-style/personal.json 存在就一并传。优先级:本次 > 员工样式配置 > personal.json。
      - 成品格式 {{ output_format | default('pptx') }} 用 --format 传入。
      - 运行技能 render.py;ok 为 false 时按 errors / qa 改内容或参数后重跑(最多 2 次),仍失败如实告知员工。
      - 按技能「检查成品」看图。
   4. save_artifact(name="{FN}.<扩展名>", path="{FN}.<扩展名>", kind="document");回复里如实说明 not_applied 与 warnings。
   ```
8. 「JSON 结构:{…}」那一行删除（结构以技能为准）。

起草后自查：全文搜 `render_plan`、`默认视觉基线`、`matplotlib`、`「注意事项」写明:当前用药` —— 应全部为 0 处。

- [ ] **Step 3: 在测试环境更新 ai-health-plan：挂载技能 + 发布 rev39**

沿用 rev38 的做法（scratchpad `ahp-opt/set_prompt38.py` 同款脚本，改名 `set_prompt39.py`）：spec 的 skills 列表追加 `health-plan-report`，`system_prompt.template` 换成 rev39 文本，保存为草稿 → 发布。发布后 `get_spec.py` 复核：技能列表含 `health-plan-report`、提示词 sha 与草稿一致。

- [ ] **Step 4: 真栈验收（测试环境）**

1. 以张女士会话（thread `acca106c-0b1c-4897-9e05-cc85071880de`）首轮输入、同一员工与客户参数，用 `accept/rp_replay.py` 跑一次新会话，`output_format=pptx`。
2. 同一会话再发一轮「字大一点，饮食那页改成表格，以后都这样」——检查：新 basename、内容 JSON 未重写、`report-style/personal.json` 出现 `type.scale` 与对应版式键。
3. 再发一轮「这次出 PDF」——检查 PDF 生成、`fonts_embedded: true`。
4. 下载三份成品，与旧版 `张女士_20260927215653.pptx` 并排出缩略图给用户；由用户在 WPS（Windows）、PowerPoint（Windows / Mac）、手机微信预览中打开核对。
5. 查 run_event：本次 run 里**没有**任何 `write_file` 写 `.py` 排版脚本、没有 `render_plan`；有且只有技能 `render.py` 调用；看图核验调用了 `read_page` → `ask_image`。

Expected: 以上全部满足；用户确认视觉。任何一项不满足：回到对应 Task 修，重新导入技能（新版本号）后重跑本步。

- [ ] **Step 5: ROADMAP 记录并提交**

在 `docs/superpowers/ROADMAP.md` 待办表加一行 **B-125 健康方案交付件平台技能 health-plan-report**（spec / 计划路径、4 个 PR 号、测试环境验收结论与日期、「生产待用户发令：导入技能 + 发布 ai-health-plan rev39，二者同时做」）。

```bash
git add docs/superpowers/ROADMAP.md
git commit -m "docs(roadmap): B-125 health-plan-report skill — live acceptance on test"
```

---

## Self-Review（计划作者已做）

- **Spec 覆盖**：§1 边界 → Global Constraints 首条 + T3 blocks 说明 + T9 `test_no_tenant_or_agent_names_anywhere`；§4 内容 → T1；§5 样式（叠加、换算、品牌锁定、可读性、个人默认、落实清单）→ T2；§6 视觉（方向 A token、页面类型、版式引擎、设计先行）→ T3/T4/T5/T6；§7 流程与质检 → T5/T7/T9；§8 迁移（含 D12 用药）→ T10；§9 测试三层 → T1–T8 + in_image + T10；§10 PR 切分 → 「PR 切分」节。
- **占位扫描**：reference 文档（T9 Step 4–6）给的是逐项内容清单而非全文——它们是说明文档，内容由代码（`KIND_SCHEMAS` / `OPTIONS` / 同义词表）唯一决定，并有测试逐项核对，不存在需要猜的内容。
- **类型一致**：`render_pptx(content, style, out_path, base_dir, m)`、`qa_pptx(path, required, theme, m)`、`qa_pdf(path, content, required, theme)`、`render_pdf(content, style, out_path, base_dir)`、`section_prims(content, style)`、`paginate(sections, ctx)`、`make_ctx(content, theme, m, base_dir)` 在定义处与调用处一致。
- **Review Focus** 五条各有对应测试（T4×2、T2、T3、T5）。
