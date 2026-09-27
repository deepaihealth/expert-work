# 平台 office 技能重写 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 从零自写平台技能 `docx` / `pptx` / `xlsx` / `pdf`（Python 路线、适配我们的沙箱），源码进 `platform-skills/`，三层测试守门，2026-09-28 随班车 2 导入生产。

**Architecture:** 每个技能 = 简短中文 `SKILL.md` + 经过测试的小脚本；共享脚本放 `platform-skills/shared/`，由各技能 `skill.yaml` 显式声明后在打包时复制进去。`build.py` 打出确定性的 `.skill` 包；第一层测试在宿主机用平台自己的导入校验代码检查包；第二层测试在真实沙箱镜像（只读根、断网）里跑每个脚本；导入走 control-plane 自己的 `_ingest_platform_skill_payload`，以自包含程序经 stdin 送进 pod 执行，成功后发 `platform_skill` 跨副本失效通知。

**Tech Stack:** Python 3.12；python-docx / python-pptx / openpyxl / pypdf / pdfplumber / pdf2image / weasyprint / matplotlib（沙箱预装）；LibreOffice `soffice`、poppler `pdftoppm`（沙箱预装）；pytest；Docker；GitHub Actions。

**Spec:** `docs/superpowers/specs/2026-09-25-platform-office-skills-design.md`（执行者必须先读 spec 全文；本计划按 spec 章节号引用）

## Global Constraints

- **clean-room**：不得打开、复制、改写 Anthropic 原版技能（`anthropics/skills` 仓库，或平台数据库里 `docx`/`pptx`/`pdf`/`xlsx` 第 1 版）的正文或脚本；只依据本计划、spec、库的公开文档编写（spec §0.2）。
- 技能名固定：`docx`、`pptx`、`xlsx`、`pdf`；目录名 = frontmatter `name`。
- frontmatter 必含：`name`、`description`（中文，≤ 200 字符）、`license: 深护智康自研，仅限本平台使用`、`expert_work: {lazy: true, category: 通用}`（spec §3.2）。
- 正文中文；代码、库名、命令英文（D8）。正文每份 ≤ 6,000 字符，目标 2,000~4,000（spec §6.1）。
- 正文与脚本**不得**出现 JS 路线：`npm`、`require(`、`pptxgenjs`、`docx-js`、`node `（spec §0.3、§6.1）。
- 脚本只用 Python 标准库 + 沙箱预装库（`SANDBOX_PREINSTALLED_PYTHON`）+ `soffice` / `pdftoppm`；不得 `pip install`，不得联网。
- 所有编辑类脚本输出到新文件：输入与输出解析后同一路径 → 退出码 1 并报错（spec §2）。
- 脚本 CLI：`argparse`；成功退出码 0；用法错误 2（argparse 默认）；运行失败 1；`recalc.py` 发现公式错误 2（spec §4.4）。结构化结果一律以**一行 JSON** 打印到 stdout，人读提示打到 stderr。
- 字体（spec §4.6 / D9）：Word / PPT 默认中文 `微软雅黑`、西文 `Arial`；PDF 默认 `Noto Sans CJK SC` 并嵌入。
- 脚本路径在正文里一律写成 `$EXPERT_WORK_SKILLS_DIR/<技能名>/scripts/<脚本>.py`。
- soffice 调用只能经 `shared/_office.py`（独立用户配置目录、进程组超时整组杀、默认 120s）。
- 本地 pytest 一律 `uv run --no-sync pytest ...`；ruff 必须通过（CI 跑 `uv run ruff check` 全仓）。
- 第二层测试本地运行需要：`export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock` 与
  `export EXPERT_WORK_SKILLS_TEST_IMAGE=crpi-sgadimluo7wm655m.cn-hangzhou.personal.cr.aliyuncs.com/expert-work/sandbox:7ac31957`
  （已拉到本机，amd64，经模拟运行，较慢属正常）。
- 提交信息结尾带：
  ```
  Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01DNzoY8vc8jDMsib4wFGFpN
  ```

## Review Focus

1. **中文文件名 / 带空格路径**（`张三 方案_20260928.docx`）：所有脚本必须正常处理 —— 各脚本任务的第二层用例都用中文带空格文件名。
2. **相对路径调用**：模型在 `/workspace` 下用相对路径调脚本（`python $EXPERT_WORK_SKILLS_DIR/docx/scripts/replace_text.py in.docx out.docx ...`），输出必须落在 `/workspace` 而不是脚本目录 —— Task 2 的 `resolve_io` 统一处理并有用例。
3. **输出目录不存在**：`--out-dir out/preview` 这类不存在的目录应自动创建，而不是报错 —— Task 2 用例。
4. **沙箱里没有 `微软雅黑`**：用它生成的 docx / pptx 预览与转 PDF 时中文必须回落成可显示字体（不是方块、PDF 文字层可提取出中文）—— Task 3 / Task 4 用例。
5. **soffice 并发与卡死**：两个转换同时跑都成功；超时后进程组被清理、不留僵尸 soffice —— Task 2 用例。

---

## 文件结构

```
platform-skills/
  README.md                         Task 1  目录说明、改动流程、发布命令
  build.py                          Task 1  打包（确定性 ZIP）
  import_in_pod.py                  Task 8  导入（本地拼装自包含程序 → pod 内执行）
  shared/
    _cli.py                         Task 2  共享的 CLI 小工具：resolve_io / emit_json / fail
    _office.py                      Task 2  soffice 唯一封装
    convert.py                      Task 2
    preview.py                      Task 2
  docx/  SKILL.md  skill.yaml  scripts/replace_text.py  scripts/fill_template.py      Task 3
  pptx/  SKILL.md  skill.yaml  scripts/inspect_template.py  scripts/duplicate_slide.py Task 4
  xlsx/  SKILL.md  skill.yaml  scripts/recalc.py                                      Task 5
  pdf/   SKILL.md  skill.yaml  scripts/pdf_ops.py                                     Task 6
  tests/
    conftest.py                     Task 1  路径常量、build 夹具
    test_build.py                   Task 1  打包规则
    test_platform_checks.py         Task 7  平台导入同款校验 + 环境事实一致 + 引用完整
    test_import_in_pod.py           Task 8
    test_in_image.py                Task 2  第二层驱动（未设环境变量则 skip）
    in_image/
      _harness.py                   Task 2  容器内用例的公共断言工具
      case_shared_*.py              Task 2
      case_docx_*.py                Task 3
      case_pptx_*.py                Task 4
      case_xlsx_*.py                Task 5
      case_pdf_*.py                 Task 6
.github/workflows/platform-skills.yml   Task 2  第二层 CI
pyproject.toml                          Task 1  testpaths 加 "platform-skills/tests"
```

---

### Task 1: 目录骨架 + `build.py` + 打包规则测试

**Files:**
- Create: `platform-skills/README.md`, `platform-skills/build.py`, `platform-skills/tests/conftest.py`, `platform-skills/tests/test_build.py`, `platform-skills/shared/.gitkeep`
- Modify: `pyproject.toml`（`[tool.pytest.ini_options].testpaths` 追加 `"platform-skills/tests"`）

**Interfaces:**
- Produces:
  - `build.py` 命令：`python platform-skills/build.py [--out DIR] [--only docx,pptx] [--root DIR]`，默认 `--out platform-skills/dist`（加进 `.gitignore`），`--root` 默认 `platform-skills/`（测试用它指向夹具目录）。
  - 函数 `build_all(root: Path, out: Path, only: set[str] | None = None) -> list[Path]`（返回生成的 `.skill` 路径，按技能名排序）。
  - 函数 `build_one(skill_dir: Path, shared_dir: Path, out: Path) -> Path`。
  - 异常 `BuildError(Exception)`。
  - 技能目录判定：`root` 下含 `SKILL.md` 的一级子目录（`shared`、`tests`、`dist` 除外）。
  - `conftest.py` 夹具：`PLATFORM_SKILLS: Path`（仓库内 `platform-skills/` 绝对路径）、`built_packages(tmp_path_factory) -> dict[str, Path]`（session 级，调用 `build_all` 一次，返回 `{name: .skill 路径}`）、`unpacked_skills(built_packages, tmp_path_factory) -> Path`（把每个包解到 `<dir>/<name>/`，模拟平台落到沙箱的样子）。

- [ ] **Step 1: 写失败测试 `platform-skills/tests/test_build.py`**

```python
import hashlib
import importlib.util
import zipfile
from pathlib import Path

import pytest

_BUILD = Path(__file__).resolve().parents[1] / "build.py"
_spec = importlib.util.spec_from_file_location("ps_build", _BUILD)
build = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build)


def _skill(root: Path, name: str, *, yaml_text: str = "", scripts: dict[str, str] | None = None) -> Path:
    d = root / name
    (d / "scripts").mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: 测试\nexpert_work:\n  lazy: true\n---\n正文\n", encoding="utf-8"
    )
    if yaml_text:
        (d / "skill.yaml").write_text(yaml_text, encoding="utf-8")
    for rel, body in (scripts or {}).items():
        (d / "scripts" / rel).write_text(body, encoding="utf-8")
    return d


@pytest.fixture
def root(tmp_path: Path) -> Path:
    r = tmp_path / "ps"
    (r / "shared").mkdir(parents=True)
    (r / "shared" / "preview.py").write_text("print('p')\n", encoding="utf-8")
    (r / "shared" / "convert.py").write_text("print('c')\n", encoding="utf-8")
    return r


def _names(pkg: Path) -> list[str]:
    with zipfile.ZipFile(pkg) as z:
        return sorted(z.namelist())


def test_declared_shared_files_are_copied_and_undeclared_are_not(root, tmp_path):
    _skill(root, "alpha", yaml_text="shared:\n  - preview.py\n", scripts={"own.py": "x=1\n"})
    [pkg] = build.build_all(root, tmp_path / "out")
    assert _names(pkg) == ["SKILL.md", "scripts/own.py", "scripts/preview.py"]


def test_no_skill_yaml_means_no_shared_files(root, tmp_path):
    _skill(root, "beta", scripts={"own.py": "x=1\n"})
    [pkg] = build.build_all(root, tmp_path / "out")
    assert _names(pkg) == ["SKILL.md", "scripts/own.py"]


def test_unknown_shared_file_fails(root, tmp_path):
    _skill(root, "alpha", yaml_text="shared:\n  - nope.py\n")
    with pytest.raises(build.BuildError, match="nope.py"):
        build.build_all(root, tmp_path / "out")


def test_unknown_yaml_key_fails(root, tmp_path):
    _skill(root, "alpha", yaml_text="shared: []\nextra: 1\n")
    with pytest.raises(build.BuildError, match="extra"):
        build.build_all(root, tmp_path / "out")


def test_own_script_shadowing_shared_fails(root, tmp_path):
    _skill(root, "alpha", yaml_text="shared:\n  - preview.py\n", scripts={"preview.py": "x=1\n"})
    with pytest.raises(build.BuildError, match="preview.py"):
        build.build_all(root, tmp_path / "out")


def test_build_is_byte_identical_across_runs(root, tmp_path):
    _skill(root, "alpha", yaml_text="shared:\n  - preview.py\n  - convert.py\n", scripts={"own.py": "x=1\n"})
    [a] = build.build_all(root, tmp_path / "o1")
    (root / "alpha" / "scripts" / "own.py").touch()  # mtime changes must not matter
    [b] = build.build_all(root, tmp_path / "o2")
    assert hashlib.sha256(a.read_bytes()).digest() == hashlib.sha256(b.read_bytes()).digest()


def test_only_filter(root, tmp_path):
    _skill(root, "alpha")
    _skill(root, "beta")
    pkgs = build.build_all(root, tmp_path / "out", only={"beta"})
    assert [p.name for p in pkgs] == ["beta.skill"]


def test_pycache_and_hidden_files_are_excluded(root, tmp_path):
    d = _skill(root, "alpha", scripts={"own.py": "x=1\n"})
    (d / "scripts" / "__pycache__").mkdir()
    (d / "scripts" / "__pycache__" / "own.cpython-312.pyc").write_bytes(b"\0")
    (d / ".DS_Store").write_bytes(b"\0")
    [pkg] = build.build_all(root, tmp_path / "out")
    assert _names(pkg) == ["SKILL.md", "scripts/own.py"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run --no-sync pytest platform-skills/tests/test_build.py -q`
Expected: FAIL（`build.py` 不存在）

- [ ] **Step 3: 实现 `platform-skills/build.py`**

```python
"""Pack platform-skills/<name>/ into deterministic <name>.skill ZIPs.

Each skill directory holds SKILL.md, optional scripts/, optional skill.yaml.
skill.yaml has exactly one key, ``shared``: files copied from
platform-skills/shared/ into the package's scripts/. Nothing is shared unless
declared (spec D6). Output is byte-identical for identical sources, so the
platform's content_hash idempotency sees "unchanged" as unchanged.
"""

from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import Path

import yaml

_EXCLUDED_DIRS = {"shared", "tests", "dist"}
_FIXED_DATE = (2026, 1, 1, 0, 0, 0)


class BuildError(Exception):
    pass


def _skill_dirs(root: Path) -> list[Path]:
    return sorted(
        d for d in root.iterdir()
        if d.is_dir() and d.name not in _EXCLUDED_DIRS and not d.name.startswith((".", "_"))
        and (d / "SKILL.md").is_file()
    )


def _read_shared_list(skill_dir: Path) -> list[str]:
    path = skill_dir / "skill.yaml"
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise BuildError(f"{path}: must be a mapping")
    unknown = set(data) - {"shared"}
    if unknown:
        raise BuildError(f"{path}: unknown keys {sorted(unknown)}")
    shared = data.get("shared") or []
    if not isinstance(shared, list) or not all(isinstance(s, str) for s in shared):
        raise BuildError(f"{path}: 'shared' must be a list of file names")
    return shared


def _package_files(skill_dir: Path, shared_dir: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for p in sorted(skill_dir.rglob("*")):
        rel = p.relative_to(skill_dir)
        if not p.is_file() or p.name == "skill.yaml":
            continue
        if any(part.startswith(".") or part == "__pycache__" for part in rel.parts):
            continue
        files[rel.as_posix()] = p.read_bytes()
    for name in _read_shared_list(skill_dir):
        src = shared_dir / name
        if not src.is_file():
            raise BuildError(f"{skill_dir.name}: declared shared file {name!r} not found in {shared_dir}")
        dest = f"scripts/{name}"
        if dest in files:
            raise BuildError(f"{skill_dir.name}: own {dest!r} would be shadowed by shared {name!r}")
        files[dest] = src.read_bytes()
    return files


def build_one(skill_dir: Path, shared_dir: Path, out: Path) -> Path:
    files = _package_files(skill_dir, shared_dir)
    out.mkdir(parents=True, exist_ok=True)
    target = out / f"{skill_dir.name}.skill"
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for rel in sorted(files):
            info = zipfile.ZipInfo(rel, date_time=_FIXED_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            z.writestr(info, files[rel])
    return target


def build_all(root: Path, out: Path, only: set[str] | None = None) -> list[Path]:
    shared_dir = root / "shared"
    dirs = [d for d in _skill_dirs(root) if only is None or d.name in only]
    return [build_one(d, shared_dir, out) for d in dirs]


def main(argv: list[str] | None = None) -> int:
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", type=Path, default=here)
    ap.add_argument("--out", type=Path, default=here / "dist")
    ap.add_argument("--only", default="")
    args = ap.parse_args(argv)
    only = {s for s in args.only.split(",") if s} or None
    try:
        pkgs = build_all(args.root, args.out, only)
    except BuildError as exc:
        print(f"build failed: {exc}", file=sys.stderr)
        return 1
    for pkg in pkgs:
        with zipfile.ZipFile(pkg) as z:
            print(f"{pkg}  {pkg.stat().st_size} bytes")
            for info in z.infolist():
                print(f"    {info.filename}  {info.file_size}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: `conftest.py`**

```python
import importlib.util
import zipfile
from pathlib import Path

import pytest

PLATFORM_SKILLS = Path(__file__).resolve().parents[1]
EXPECTED_SKILLS = ("docx", "pptx", "xlsx", "pdf")


def _load_build():
    spec = importlib.util.spec_from_file_location("ps_build_conftest", PLATFORM_SKILLS / "build.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="session")
def built_packages(tmp_path_factory) -> dict[str, Path]:
    out = tmp_path_factory.mktemp("dist")
    return {p.stem: p for p in _load_build().build_all(PLATFORM_SKILLS, out)}


@pytest.fixture(scope="session")
def unpacked_skills(built_packages, tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("skills")
    for name, pkg in built_packages.items():
        with zipfile.ZipFile(pkg) as z:
            z.extractall(root / name)
    return root
```

- [ ] **Step 5: `pyproject.toml` testpaths 追加 `"platform-skills/tests"`；`.gitignore` 追加 `platform-skills/dist/`**

- [ ] **Step 6: `README.md`** —— 写清：目录用途（平台自写技能源码，clean-room，禁止参考 Anthropic 原版）、目录结构、`skill.yaml` 规则、本地打包 `python platform-skills/build.py`、三层测试怎么跑（含两个环境变量）、发布命令（占位引用 Task 8 的 `import_in_pod.py` 用法，Task 8 完成时补全命令）。README 是 Markdown，不是占位：Task 8 的 Step 里有「补全 README 发布一节」的具体文本。

- [ ] **Step 7: 跑测试确认通过；ruff**

Run: `uv run --no-sync pytest platform-skills/tests/test_build.py -q && uv run --no-sync ruff check platform-skills && uv run --no-sync ruff format --check platform-skills`
Expected: 全部 PASS

- [ ] **Step 8: 自证测试咬得住** —— 把 `_package_files` 里的「同名报错」两行注释掉，`test_own_script_shadowing_shared_fails` 必须变红；还原（`git diff` 确认已还原）后变绿。

- [ ] **Step 9: Commit**

```bash
git add platform-skills/ pyproject.toml .gitignore
git commit -m "feat(platform-skills): 目录骨架 + 确定性打包 build.py(共享脚本按声明分发)"
```

---

### Task 2: 共享脚本（`_cli` / `_office` / `convert` / `preview`）+ 第二层测试框架 + CI

**Files:**
- Create: `platform-skills/shared/_cli.py`, `_office.py`, `convert.py`, `preview.py`
- Create: `platform-skills/tests/test_in_image.py`, `platform-skills/tests/in_image/_harness.py`, `platform-skills/tests/in_image/case_shared_convert.py`, `case_shared_preview.py`, `case_shared_office_timeout.py`
- Create: `.github/workflows/platform-skills.yml`

**Interfaces:**
- Consumes: Task 1 `build_all`、`unpacked_skills` 夹具。
- Produces（在沙箱里以 `$EXPERT_WORK_SKILLS_DIR/<skill>/scripts/` 下同目录模块互相导入：脚本开头 `sys.path.insert(0, str(Path(__file__).resolve().parent))`）：
  - `_cli.resolve_io(inp: str, out: str | None) -> tuple[Path, Path | None]`：相对路径按**当前工作目录**解析；输入不存在 → `fail`；输出父目录自动创建；输入输出 `resolve()` 后相同 → `fail("输出不能覆盖原文件")`。
  - `_cli.emit_json(obj) -> None`（`json.dumps(obj, ensure_ascii=False)` 一行到 stdout）。
  - `_cli.fail(msg: str, code: int = 1) -> NoReturn`（`msg` 到 stderr，`sys.exit(code)`）。
  - `_office.run_soffice(args: list[str], *, timeout: float = 120.0, recalc: bool = False) -> subprocess.CompletedProcess`：独立配置目录、进程组、超时整组 `SIGKILL`，超时抛 `OfficeTimeout(TimeoutError)`；`recalc=True` 时在配置目录写入强制重算设置（Task 5 用）。
  - `_office.to_pdf(src: Path, out_dir: Path, *, timeout: float = 120.0) -> Path`。
  - `_office.convert(src: Path, fmt: str, out_dir: Path, *, timeout: float = 120.0, recalc: bool = False) -> Path`。
  - `convert.py IN --to pdf|docx|pptx|xlsx [--out-dir DIR] [--timeout 120]` → stdout JSON `{"output": "<path>"}`。
  - `preview.py IN [--out-dir DIR] [--pages 1-3|all|1,3,5] [--dpi 110]` → stdout JSON `{"pages_total": N, "images": ["<png>", ...]}`；默认 `--out-dir <IN 的文件名去扩展名>_preview`、`--pages 1-3`。
  - `tests/in_image/_harness.py`：`run(cmd: list[str], *, expect: int = 0, timeout: float = 300) -> subprocess.CompletedProcess`、`script(skill: str, name: str) -> str`（返回 `$EXPERT_WORK_SKILLS_DIR/<skill>/scripts/<name>` 绝对路径）、`check(cond: bool, msg: str)`（失败即 `sys.exit(1)` 并打印 msg）、`png_is_not_blank(path) -> bool`（灰度方差 > 20）。
  - `test_in_image.py`：对 `in_image/case_*.py` 参数化，每个 case 一条 pytest；未设 `EXPERT_WORK_SKILLS_TEST_IMAGE` 则整体 skip。

- [ ] **Step 1: 写 `_harness.py` 与三个共享用例（它们就是失败测试）**

`platform-skills/tests/in_image/_harness.py`:
```python
"""Helpers for cases that run INSIDE the sandbox image (stdlib + preinstalled libs only)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def script(skill: str, name: str) -> str:
    return str(Path(os.environ["EXPERT_WORK_SKILLS_DIR"]) / skill / "scripts" / name)


def run(cmd: list[str], *, expect: int = 0, timeout: float = 300) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)  # noqa: S603
    if proc.returncode != expect:
        print(f"FAIL: {cmd} -> {proc.returncode} (expected {expect})\nstdout={proc.stdout}\nstderr={proc.stderr}")
        sys.exit(1)
    return proc


def check(cond: bool, msg: str) -> None:
    if not cond:
        print(f"FAIL: {msg}")
        sys.exit(1)


def png_is_not_blank(path: Path) -> bool:
    from PIL import Image, ImageStat

    with Image.open(path) as im:
        return ImageStat.Stat(im.convert("L")).var[0] > 20
```

`case_shared_convert.py`（在 `/workspace` 下、中文带空格文件名、相对路径；docx/pptx/xlsx → pdf；并发两路）:
```python
import json
import os
import threading
from pathlib import Path

import docx
import openpyxl
import pptx
from _harness import check, run, script

os.chdir("/workspace")
d = docx.Document(); d.add_paragraph("中文段落 测试"); d.save("张三 方案.docx")
p = pptx.Presentation(); s = p.slides.add_slide(p.slide_layouts[1]); s.shapes.title.text = "中文标题"; p.save("汇报 一.pptx")
wb = openpyxl.Workbook(); wb.active["A1"] = "中文"; wb.save("数据 表.xlsx")

for name in ("张三 方案.docx", "汇报 一.pptx", "数据 表.xlsx"):
    out = json.loads(run(["python", script("docx", "convert.py"), name, "--to", "pdf", "--out-dir", "out/pdf"]).stdout)
    pdf = Path(out["output"])
    check(pdf.is_file() and pdf.stat().st_size > 1000, f"no pdf for {name}")
    check(pdf.resolve().is_relative_to(Path("/workspace/out/pdf")), f"pdf not under /workspace/out/pdf: {pdf}")

results = []
def _one(src: str, sub: str) -> None:
    results.append(run(["python", script("docx", "convert.py"), src, "--to", "pdf", "--out-dir", sub]).returncode)
ts = [threading.Thread(target=_one, args=("张三 方案.docx", f"par{i}")) for i in range(2)]
[t.start() for t in ts]; [t.join() for t in ts]
check(results == [0, 0], f"concurrent conversions: {results}")
run(["python", script("docx", "convert.py"), "张三 方案.docx", "--to", "docx", "--out-dir", "."], expect=1)
print("PASS case_shared_convert")
```

`case_shared_preview.py`:
```python
import json
import os
from pathlib import Path

import docx
from _harness import check, png_is_not_blank, run, script

os.chdir("/workspace")
d = docx.Document()
for i in range(5):
    d.add_heading(f"第 {i + 1} 页 标题", 1); d.add_paragraph("正文内容 " * 50)
    if i < 4:
        d.add_page_break()
d.save("长 文档.docx")

res = json.loads(run(["python", script("docx", "preview.py"), "长 文档.docx"]).stdout)
check(res["pages_total"] == 5, f"pages_total={res['pages_total']}")
check(len(res["images"]) == 3, f"default should render 3 pages, got {len(res['images'])}")
for img in res["images"]:
    check(Path(img).is_file() and png_is_not_blank(Path(img)), f"blank or missing {img}")
check(Path(res["images"][0]).resolve().parent == Path("/workspace/长 文档_preview"), f"default out dir wrong: {res['images'][0]}")

res = json.loads(run(["python", script("docx", "preview.py"), "长 文档.docx", "--pages", "all", "--out-dir", "a/b/c"]).stdout)
check(len(res["images"]) == 5, "all pages")
res = json.loads(run(["python", script("docx", "preview.py"), "长 文档.docx", "--pages", "2,4", "--out-dir", "sel"]).stdout)
check(len(res["images"]) == 2, "selected pages")
run(["python", script("docx", "preview.py"), "不存在.docx"], expect=1)
print("PASS case_shared_preview")
```

`case_shared_office_timeout.py`（直接导入 `_office`，给 1s 超时，断言抛 `OfficeTimeout` 且之后没有残留 soffice 进程）:
```python
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(os.environ["EXPERT_WORK_SKILLS_DIR"]) / "docx" / "scripts"))
import docx
import _office
from _harness import check

os.chdir("/workspace")
d = docx.Document(); d.add_paragraph("x" * 10); d.save("t.docx")
raised = False
try:
    _office.to_pdf(Path("t.docx"), Path("to"), timeout=1.0)
except _office.OfficeTimeout:
    raised = True
check(raised, "expected OfficeTimeout with 1s timeout (cold soffice start takes longer)")
time.sleep(2)
ps = subprocess.run(["ps", "-eo", "comm"], capture_output=True, text=True).stdout
check("soffice" not in ps, f"soffice left running:\n{ps}")
print("PASS case_shared_office_timeout")
```

> 注意：`case_shared_office_timeout` 依赖「soffice 冷启动 > 1s」。若沙箱里偶发 1s 内完成，该用例会假失败 —— 实现时先在镜像里实测冷启动耗时，若 < 3s，把用例改为转换一个足够大的文档（例如 300 页的 docx）并记录实测值于用例注释。

- [ ] **Step 2: 写驱动 `platform-skills/tests/test_in_image.py`**

```python
"""Layer 2 (spec §6.2): run in_image/case_*.py inside the real sandbox image.

Skipped unless EXPERT_WORK_SKILLS_TEST_IMAGE is set. Mirrors production
sandbox conditions: read-only root, no network, writable /workspace, /tmp,
/home/agent; skills materialized under /opt/skills/<name>/.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

IMAGE = os.environ.get("EXPERT_WORK_SKILLS_TEST_IMAGE", "")
CASES_DIR = Path(__file__).parent / "in_image"
CASES = sorted(p.name for p in CASES_DIR.glob("case_*.py"))

pytestmark = pytest.mark.skipif(not IMAGE, reason="EXPERT_WORK_SKILLS_TEST_IMAGE not set (layer-2 tests)")


@pytest.mark.parametrize("case", CASES)
def test_case_in_sandbox_image(case: str, unpacked_skills: Path) -> None:
    docker = shutil.which("docker")
    assert docker, "docker not found"
    cmd = [
        docker, "run", "--rm", "--platform", "linux/amd64",
        "--read-only", "--network", "none",
        "--tmpfs", "/tmp:rw,size=512m,mode=1777",
        "--tmpfs", "/home/agent:rw,size=256m,mode=1777",
        "--tmpfs", "/workspace:rw,size=512m,mode=1777",
        "-v", f"{unpacked_skills}:/opt/skills:ro",
        "-v", f"{CASES_DIR}:/opt/cases:ro",
        "-e", "EXPERT_WORK_SKILLS_DIR=/opt/skills",
        "-e", "PYTHONPATH=/opt/cases",
        "-w", "/workspace",
        "--entrypoint", "python",
        IMAGE, f"/opt/cases/{case}",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900, check=False)  # noqa: S603
    assert proc.returncode == 0, f"{case}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    assert f"PASS {case.removesuffix('.py')}" in proc.stdout
```

- [ ] **Step 3: 跑第二层确认失败**

Run: `uv run --no-sync pytest platform-skills/tests/test_in_image.py -q`（带 Global Constraints 里的两个环境变量）
Expected: FAIL —— 此时 `docx` 技能还不存在、共享脚本也不存在。为了让共享用例先能跑，本任务在 `platform-skills/docx/` 放一个**临时最小** `SKILL.md` + `skill.yaml`（`shared: [_cli.py, _office.py, convert.py, preview.py]`），Task 3 会整份覆写它。临时 SKILL.md 正文写「占位，Task 3 覆写」，Task 7 的正文长度 / 内容检查会在 Task 3 之后才加入断言 docx 正文完整。

- [ ] **Step 4: 实现 `_cli.py`**

```python
"""Tiny CLI helpers shared by platform-skill scripts (stdlib only)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, NoReturn


def fail(msg: str, code: int = 1) -> NoReturn:
    print(msg, file=sys.stderr)
    sys.exit(code)


def emit_json(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False))


def resolve_io(inp: str, out: str | None) -> tuple[Path, Path | None]:
    src = Path(inp).expanduser()
    src = (Path.cwd() / src) if not src.is_absolute() else src
    if not src.is_file():
        fail(f"找不到输入文件：{inp}")
    if out is None:
        return src, None
    dst = Path(out).expanduser()
    dst = (Path.cwd() / dst) if not dst.is_absolute() else dst
    if dst.resolve() == src.resolve():
        fail("输出不能覆盖原文件：请换一个输出文件名")
    dst.parent.mkdir(parents=True, exist_ok=True)
    return src, dst
```

- [ ] **Step 5: 实现 `_office.py`**

```python
"""The single place that runs LibreOffice (soffice) headless.

Each call gets its own user-profile dir (parallel runs would otherwise lock
each other out), runs in its own process group, and on timeout the whole
group is SIGKILLed so no orphan soffice keeps the sandbox busy.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import tempfile
from pathlib import Path

_RECALC_XCU = """<?xml version="1.0" encoding="UTF-8"?>
<oor:items xmlns:oor="http://openoffice.org/2001/registry" xmlns:xs="http://www.w3.org/2001/XMLSchema" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
<item oor:path="/org.openoffice.Office.Calc/Formula/Load"><prop oor:name="OOXMLRecalcMode" oor:op="fuse"><value>0</value></prop></item>
<item oor:path="/org.openoffice.Office.Calc/Formula/Load"><prop oor:name="ODFRecalcMode" oor:op="fuse"><value>0</value></prop></item>
</oor:items>
"""

_FORMATS = {"pdf", "docx", "pptx", "xlsx"}


class OfficeError(RuntimeError):
    pass


class OfficeTimeout(TimeoutError):
    pass


def run_soffice(args: list[str], *, timeout: float = 120.0, recalc: bool = False) -> subprocess.CompletedProcess:
    exe = shutil.which("soffice") or shutil.which("libreoffice")
    if exe is None:
        raise OfficeError("沙箱里找不到 soffice（LibreOffice）")
    profile = Path(tempfile.mkdtemp(prefix="lo-profile-"))
    try:
        if recalc:
            user = profile / "user"
            user.mkdir(parents=True)
            (user / "registrymodifications.xcu").write_text(_RECALC_XCU, encoding="utf-8")
        cmd = [exe, f"-env:UserInstallation={profile.as_uri()}", "--headless", "--norestore",
               "--nolockcheck", "--nodefault", "--nologo", *args]
        proc = subprocess.Popen(  # noqa: S603
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True
        )
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate()
            raise OfficeTimeout(f"转换超时 {timeout:.0f}s：文件可能过大或损坏") from None
        return subprocess.CompletedProcess(cmd, proc.returncode, out, err)
    finally:
        shutil.rmtree(profile, ignore_errors=True)


def convert(src: Path, fmt: str, out_dir: Path, *, timeout: float = 120.0, recalc: bool = False) -> Path:
    if fmt not in _FORMATS:
        raise OfficeError(f"不支持转换成 {fmt}")
    if src.suffix.lower().lstrip(".") == fmt:
        raise OfficeError(f"输入已经是 .{fmt}，不需要转换")
    out_dir.mkdir(parents=True, exist_ok=True)
    proc = run_soffice(["--convert-to", fmt, "--outdir", str(out_dir), str(src)], timeout=timeout, recalc=recalc)
    target = out_dir / f"{src.stem}.{fmt}"
    if proc.returncode != 0 or not target.is_file():
        raise OfficeError(f"转换失败（退出码 {proc.returncode}）：{proc.stderr.strip()[-500:]}")
    return target


def to_pdf(src: Path, out_dir: Path, *, timeout: float = 120.0) -> Path:
    return convert(src, "pdf", out_dir, timeout=timeout)
```

- [ ] **Step 6: 实现 `convert.py` 与 `preview.py`**

`convert.py`:
```python
"""Convert office files with LibreOffice: docx/pptx/xlsx -> pdf, doc -> docx, ppt -> pptx, xls -> xlsx."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _office  # noqa: E402
from _cli import emit_json, fail, resolve_io  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("input")
    ap.add_argument("--to", required=True, choices=["pdf", "docx", "pptx", "xlsx"])
    ap.add_argument("--out-dir", default=".")
    ap.add_argument("--timeout", type=float, default=120.0)
    args = ap.parse_args()
    src, _ = resolve_io(args.input, None)
    out_dir = Path(args.out_dir)
    out_dir = out_dir if out_dir.is_absolute() else Path.cwd() / out_dir
    try:
        out = _office.convert(src, args.to, out_dir, timeout=args.timeout)
    except (_office.OfficeError, _office.OfficeTimeout) as exc:
        fail(str(exc))
    emit_json({"output": str(out)})


if __name__ == "__main__":
    main()
```

`preview.py`:
```python
"""Render pages of a docx/pptx/xlsx/pdf (and doc/ppt/xls) to PNG for visual checking."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _office  # noqa: E402
from _cli import emit_json, fail, resolve_io  # noqa: E402
from pypdf import PdfReader  # noqa: E402


def _parse_pages(spec: str, total: int) -> list[int]:
    if spec == "all":
        return list(range(1, total + 1))
    pages: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            pages.update(range(int(a), int(b) + 1))
        elif part:
            pages.add(int(part))
    return sorted(p for p in pages if 1 <= p <= total)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("input")
    ap.add_argument("--out-dir")
    ap.add_argument("--pages", default="1-3")
    ap.add_argument("--dpi", type=int, default=110)
    ap.add_argument("--timeout", type=float, default=120.0)
    args = ap.parse_args()
    src, _ = resolve_io(args.input, None)
    out_dir = Path(args.out_dir) if args.out_dir else Path.cwd() / f"{src.stem}_preview"
    out_dir = out_dir if out_dir.is_absolute() else Path.cwd() / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="preview-"))
    try:
        if src.suffix.lower() == ".pdf":
            pdf = src
        else:
            try:
                pdf = _office.to_pdf(src, tmp, timeout=args.timeout)
            except (_office.OfficeError, _office.OfficeTimeout) as exc:
                fail(str(exc))
        total = len(PdfReader(str(pdf)).pages)
        try:
            pages = _parse_pages(args.pages, total)
        except ValueError:
            fail(f"--pages 格式不对：{args.pages}（例：1-3、2,4、all）", 2)
        images: list[str] = []
        for n in pages:
            prefix = out_dir / f"page-{n:03d}"
            subprocess.run(  # noqa: S603
                ["pdftoppm", "-png", "-r", str(args.dpi), "-f", str(n), "-l", str(n), "-singlefile",
                 str(pdf), str(prefix)],
                check=True, capture_output=True, timeout=args.timeout,
            )
            images.append(str(prefix.with_suffix(".png")))
        emit_json({"pages_total": total, "images": images})
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
```

- [ ] **Step 7: CI workflow `.github/workflows/platform-skills.yml`**

```yaml
name: Platform skills (in sandbox image)

on:
  pull_request:
    branches: [main]
    paths:
      - "platform-skills/**"
      - "infra/sandbox-image/**"
      - ".github/workflows/platform-skills.yml"
  push:
    branches: [main]
    paths:
      - "platform-skills/**"
      - "infra/sandbox-image/**"
      - ".github/workflows/platform-skills.yml"
  workflow_dispatch:

concurrency:
  group: ${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: ${{ github.event_name == 'pull_request' }}

permissions:
  contents: read

jobs:
  in-image:
    name: Platform skills — layer 2 (real sandbox image)
    runs-on: ubuntu-latest
    timeout-minutes: 90
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
      - name: Set up Buildx
        uses: docker/setup-buildx-action@f87e5991a6d7451dcb8d9637bfbc97413f497069 # v4.4.1
      - name: Build sandbox image (load local, shared GHA cache)
        uses: docker/build-push-action@c3c9e263c25d99ce0380d002d59b67737d91b0dc # v7.4.0
        with:
          context: infra/sandbox-image
          file: infra/sandbox-image/Dockerfile
          load: true
          tags: expert-work-sandbox:ci
          cache-from: type=gha
      - uses: astral-sh/setup-uv@v7
        with:
          version: "0.9.26"   # 与 ci.yml 的 env.UV_VERSION 一致
          enable-cache: true
      - run: uv sync --frozen
      - name: Layer-2 tests
        env:
          EXPERT_WORK_SKILLS_TEST_IMAGE: expert-work-sandbox:ci
        run: uv run pytest platform-skills/tests/test_in_image.py -v --timeout=1800 --timeout-method=thread
```

action 版本与仓库现有 workflow 一致（checkout 取自 `sandbox-image.yml:71`，setup-uv 与 `UV_VERSION` 取自 `ci.yml:15,76-79`）；实现时若这两处已变，以当时文件为准。`cache-from` 只读不写，避免与 `sandbox-image.yml` 的缓存互相覆盖。

- [ ] **Step 8: 跑第二层确认三个共享用例通过；ruff**

Run: `uv run --no-sync pytest platform-skills/tests/test_in_image.py -q -k shared`（带环境变量）
Expected: 3 passed

- [ ] **Step 9: 自证** —— 把 `_office.run_soffice` 的 `os.killpg(...)` 改成 `proc.kill()`，`case_shared_office_timeout` 必须因残留进程变红（若不变红，说明 soffice 进程没有派生子进程；在用例注释里记录实测结论并改为断言「无 soffice.bin 残留」）；还原并 `git diff` 确认。

- [ ] **Step 10: Commit**

```bash
git add platform-skills/shared platform-skills/tests .github/workflows/platform-skills.yml platform-skills/docx
git commit -m "feat(platform-skills): 共享脚本 convert/preview/_office + 第二层沙箱镜像测试框架与 CI"
```

---

### Task 3: `docx` 技能

**Files:**
- Create/overwrite: `platform-skills/docx/SKILL.md`, `platform-skills/docx/skill.yaml`
- Create: `platform-skills/docx/scripts/replace_text.py`, `platform-skills/docx/scripts/fill_template.py`
- Create: `platform-skills/tests/in_image/case_docx_create.py`, `case_docx_replace.py`, `case_docx_template.py`

**Interfaces:**
- Consumes: Task 2 `_cli`（`resolve_io` / `emit_json` / `fail`）、`preview.py`、`convert.py`、`_harness`。
- Produces:
  - `skill.yaml`：`shared: [_cli.py, _office.py, convert.py, preview.py]`
  - `replace_text.py IN.docx OUT.docx --rules RULES.json` → stdout `{"rules": [{"find": str, "replace": str, "count": int}], "unmatched": [str]}`
  - `fill_template.py TEMPLATE.docx --list` → `{"placeholders": [str]}`；`fill_template.py TEMPLATE.docx OUT.docx --data DATA.json` → `{"filled": [str], "missing": [str], "unused": [str]}`
  - 共享的段落遍历器（写在 `replace_text.py` 里，`fill_template.py` 从它导入）：`iter_paragraphs(document) -> Iterator[Paragraph]` 覆盖正文、表格（含嵌套）、每节页眉页脚（含首页、偶数页）。
  - 共享的替换内核：`replace_in_paragraph(paragraph, find: str, replace: str) -> int`。

- [ ] **Step 1: 写第二层用例（失败测试）**

`case_docx_replace.py`（核心：一句话拆成三个 run、首个 run 加粗；表格、页眉里的目标；同路径拒绝；0 命中报告）:
```python
import json
import os

import docx
from _harness import check, run, script

os.chdir("/workspace")
d = docx.Document()
p = d.add_paragraph()
r1 = p.add_run("客户"); r1.bold = True
p.add_run("姓名：")
p.add_run("张三")
t = d.add_table(rows=1, cols=1); t.cell(0, 0).paragraphs[0].add_run("客户姓名：张三")
inner = t.cell(0, 0).add_table(rows=1, cols=1); inner.cell(0, 0).paragraphs[0].add_run("嵌套 张三")
d.sections[0].header.paragraphs[0].add_run("页眉 张三")
d.save("原 件.docx")
with open("rules.json", "w", encoding="utf-8") as f:
    json.dump([{"find": "客户姓名：张三", "replace": "客户姓名：李四"}, {"find": "张三", "replace": "李四"},
               {"find": "不存在的词", "replace": "x"}], f, ensure_ascii=False)
res = json.loads(run(["python", script("docx", "replace_text.py"), "原 件.docx", "输出/新 件.docx", "--rules", "rules.json"]).stdout)
check(res["unmatched"] == ["不存在的词"], f"unmatched={res['unmatched']}")
out = docx.Document("输出/新 件.docx")
first = out.paragraphs[0]
check(first.text == "客户姓名：李四", f"split-run replace wrong: {first.text!r}")
check(first.runs[0].bold is True, "first run lost bold")
cell = out.tables[0].cell(0, 0)
check(cell.paragraphs[0].text == "客户姓名：李四", f"table text {cell.paragraphs[0].text!r}")
check(cell.tables[0].cell(0, 0).paragraphs[0].text == "嵌套 李四", "nested table")
check(out.sections[0].header.paragraphs[0].text == "页眉 李四", "header")
check(docx.Document("原 件.docx").paragraphs[0].text == "客户姓名：张三", "original was modified")
run(["python", script("docx", "replace_text.py"), "原 件.docx", "原 件.docx", "--rules", "rules.json"], expect=1)
print("PASS case_docx_replace")
```

`case_docx_template.py`（`--list`；占位符跨 run；缺值与多余键报告）:
```python
import json
import os

import docx
from _harness import check, run, script

os.chdir("/workspace")
d = docx.Document()
p = d.add_paragraph(); p.add_run("尊敬的 {{客户"); p.add_run("名}}，您好")
d.add_paragraph("机构：{{机构名}}  日期：{{日期}}")
d.save("模板.docx")
res = json.loads(run(["python", script("docx", "fill_template.py"), "模板.docx", "--list"]).stdout)
check(sorted(res["placeholders"]) == ["客户名", "日期", "机构名"], f"placeholders={res}")
with open("data.json", "w", encoding="utf-8") as f:
    json.dump({"客户名": "李四", "机构名": "深护智康", "多余": 1}, f, ensure_ascii=False)
res = json.loads(run(["python", script("docx", "fill_template.py"), "模板.docx", "成品.docx", "--data", "data.json"]).stdout)
check(res["missing"] == ["日期"] and res["unused"] == ["多余"], f"report={res}")
text = "\n".join(pp.text for pp in docx.Document("成品.docx").paragraphs)
check("尊敬的 李四，您好" in text and "机构：深护智康" in text and "{{日期}}" in text, text)
print("PASS case_docx_template")
```

`case_docx_create.py`：把 `SKILL.md` 里「新建」一节的骨架代码**原样**抽出执行（用例读取 `$EXPERT_WORK_SKILLS_DIR/docx/SKILL.md`，取第一个标注为 ```` ```python title=skeleton ```` 的代码块，`exec` 到 `/workspace` 下，骨架必须生成 `骨架 示例.docx`），然后：
```python
import json
import os
import re
from pathlib import Path

import docx
from docx.oxml.ns import qn
from _harness import check, run, script

os.chdir("/workspace")
body = (Path(os.environ["EXPERT_WORK_SKILLS_DIR"]) / "docx" / "SKILL.md").read_text(encoding="utf-8")
m = re.search(r"```python title=skeleton\n(.*?)```", body, re.S)
check(m is not None, "SKILL.md has no ```python title=skeleton block")
exec(compile(m.group(1), "skeleton", "exec"), {})  # noqa: S102
out = Path("骨架 示例.docx")
check(out.is_file(), "skeleton did not produce 骨架 示例.docx")
doc = docx.Document(str(out))
rfonts = doc.styles["Normal"].element.rPr.rFonts
check(rfonts.get(qn("w:eastAsia")) == "微软雅黑", f"eastAsia font={rfonts.get(qn('w:eastAsia'))}")
check(len(doc.tables) >= 1 and len(doc.inline_shapes) >= 1, "skeleton must show a table and a picture")
pdf = json.loads(run(["python", script("docx", "convert.py"), str(out), "--to", "pdf", "--out-dir", "pdf"]).stdout)["output"]
import pdfplumber
with pdfplumber.open(pdf) as pp:
    text = "".join(pg.extract_text() or "" for pg in pp.pages)
check("中文" in text or "方案" in text, f"CJK text not extractable after 微软雅黑 fallback: {text[:200]!r}")
res = json.loads(run(["python", script("docx", "preview.py"), str(out)]).stdout)
check(res["images"], "preview produced nothing")
print("PASS case_docx_create")
```

- [ ] **Step 2: 跑确认失败**

Run: `uv run --no-sync pytest platform-skills/tests/test_in_image.py -q -k docx`
Expected: FAIL（脚本不存在、骨架代码块不存在）

- [ ] **Step 3: 实现 `replace_text.py`**

```python
"""Replace text in a .docx while keeping formatting (body, tables incl. nested, headers/footers)."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterator
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import docx  # noqa: E402
from _cli import emit_json, fail, resolve_io  # noqa: E402
from docx.table import Table  # noqa: E402
from docx.text.paragraph import Paragraph  # noqa: E402


def _iter_block(container) -> Iterator[Paragraph]:
    yield from container.paragraphs
    for table in container.tables:
        yield from _iter_table(table)


def _iter_table(table: Table) -> Iterator[Paragraph]:
    for row in table.rows:
        for cell in row.cells:
            yield from _iter_block(cell)


def iter_paragraphs(document) -> Iterator[Paragraph]:
    yield from _iter_block(document)
    for section in document.sections:
        for part in (section.header, section.footer, section.first_page_header, section.first_page_footer,
                     section.even_page_header, section.even_page_footer):
            if part is not None and not part.is_linked_to_previous:
                yield from _iter_block(part)


def replace_in_paragraph(paragraph: Paragraph, find: str, replace: str) -> int:
    """Replace every occurrence of ``find`` even when it spans several runs.

    The replaced text lands in the run where the match starts (keeping that
    run's formatting); the consumed parts of the following runs are removed.
    """
    if not find:
        return 0
    count = 0
    while True:
        runs = paragraph.runs
        texts = [r.text for r in runs]
        full = "".join(texts)
        idx = full.find(find)
        if idx < 0:
            return count
        starts = []
        pos = 0
        for t in texts:
            starts.append(pos)
            pos += len(t)
        end = idx + len(find)
        first = max(i for i, s in enumerate(starts) if s <= idx and (s < end or len(find) == 0))
        for i, run in enumerate(runs):
            s, e = starts[i], starts[i] + len(texts[i])
            if e <= idx or s >= end:
                continue
            keep_before = texts[i][: max(0, idx - s)]
            keep_after = texts[i][max(0, end - s):] if e > end else ""
            run.text = keep_before + (replace if i == first else "") + keep_after
        count += 1


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("input")
    ap.add_argument("output")
    ap.add_argument("--rules", required=True, help='JSON 文件：[{"find": "...", "replace": "..."}]')
    args = ap.parse_args()
    src, dst = resolve_io(args.input, args.output)
    try:
        rules = json.loads(Path(args.rules).read_text(encoding="utf-8"))
        pairs = [(str(r["find"]), str(r["replace"])) for r in rules]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        fail(f"--rules 读取失败（需要 [{{\"find\": ..., \"replace\": ...}}]）：{exc}", 2)
    document = docx.Document(str(src))
    counts = [0] * len(pairs)
    for paragraph in list(iter_paragraphs(document)):
        for i, (find, repl) in enumerate(pairs):
            counts[i] += replace_in_paragraph(paragraph, find, repl)
    document.save(str(dst))
    emit_json({
        "rules": [{"find": f, "replace": r, "count": c} for (f, r), c in zip(pairs, counts, strict=True)],
        "unmatched": [f for (f, _), c in zip(pairs, counts, strict=True) if c == 0],
    })


if __name__ == "__main__":
    main()
```

> 规则按列表顺序、对每个段落依次应用：用例里「客户姓名：张三」排在「张三」前面，因此先整体替换。`first` 的选取：包含匹配起点的那个 run（`starts[i] <= idx`，取最后一个满足者）。实现后以 `case_docx_replace` 为准；若首 run 为空串导致选错，修正选取逻辑并在该用例加一条「首 run 为空」的断言。

- [ ] **Step 4: 实现 `fill_template.py`**（`{{key}}`，key 允许中文、字母、数字、下划线；占位符可跨 run —— 复用 `replace_in_paragraph`；`--list` 在「拼接后的段落文本」上用正则 `\{\{\s*([^{}\s]+)\s*\}\}` 收集；填值时对每个 key 调 `replace_in_paragraph(p, "{{key}}", value)`，并额外处理 `{{ key }}` 带空格写法；`missing` = 模板有但 data 无；`unused` = data 有但模板无；输出排序后的列表）。完整代码：

```python
"""Fill {{placeholders}} in a .docx template, or list them."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import docx  # noqa: E402
from _cli import emit_json, fail, resolve_io  # noqa: E402
from replace_text import iter_paragraphs, replace_in_paragraph  # noqa: E402

_PH = re.compile(r"\{\{\s*([^{}\s]+)\s*\}\}")


def _placeholders(document) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for p in iter_paragraphs(document):
        for m in _PH.finditer(p.text):
            found.setdefault(m.group(1), set()).add(m.group(0))
    return found


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("template")
    ap.add_argument("output", nargs="?")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--data", help="JSON 文件：{\"占位符名\": \"值\"}")
    args = ap.parse_args()
    if args.list:
        src, _ = resolve_io(args.template, None)
        emit_json({"placeholders": sorted(_placeholders(docx.Document(str(src))))})
        return
    if not args.output or not args.data:
        fail("填充需要 输出文件 与 --data；只想看占位符用 --list", 2)
    src, dst = resolve_io(args.template, args.output)
    try:
        data = json.loads(Path(args.data).read_text(encoding="utf-8"))
        assert isinstance(data, dict)
    except (OSError, ValueError, AssertionError) as exc:
        fail(f"--data 需要一个 JSON 对象：{exc}", 2)
    document = docx.Document(str(src))
    found = _placeholders(document)
    for key, spellings in found.items():
        if key not in data:
            continue
        for p in list(iter_paragraphs(document)):
            for spelling in spellings:
                replace_in_paragraph(p, spelling, str(data[key]))
    document.save(str(dst))
    emit_json({
        "filled": sorted(k for k in found if k in data),
        "missing": sorted(k for k in found if k not in data),
        "unused": sorted(k for k in data if k not in found),
    })


if __name__ == "__main__":
    main()
```

- [ ] **Step 5: 写 `SKILL.md`**（按 spec §4.1 骨架八节，全部中文；≤ 6,000 字符）。必须包含的内容与**逐字**要求：
  1. frontmatter：
     ```
     ---
     name: docx
     description: 新建、修改或转换 Word 文档（.docx）时使用：方案、报告、合同、按机构模板出文档、转 PDF。只读取内容用 read_document，不用本技能。
     license: 深护智康自研，仅限本平台使用
     expert_work:
       lazy: true
       category: 通用
     ---
     ```
  2. 「环境」一节，四个技能**逐字相同**的这段（Task 7 会断言四份一致）：
     ```
     ## 环境
     - 已预装、直接用、不要装：python-docx、python-pptx、openpyxl、pypdf、pdfplumber、pdf2image、weasyprint、matplotlib、Pillow、pandas；命令行 soffice（LibreOffice）、pdftoppm；中文字体 Noto Sans CJK。
     - 需要别的 Python 包时可以 pip install（走国内镜像，很快），但沙箱空闲后会回收、装过的包会丢；本技能不依赖任何需要现装的包。
     - 没有 npm：不要走任何 JavaScript 路线。
     - 工作目录 /workspace；本技能脚本在 $EXPERT_WORK_SKILLS_DIR/docx/scripts/。
     - 成品必须用 save_artifact 登记，否则用户拿不到。
     - 只读取已有文档内容用 read_document / read_page。
     ```
     （pptx / xlsx / pdf 三份里只把 `docx` 换成各自技能名；Task 7 的一致性断言按此规则比对。）
  3. 「新建」一节含一个 ```` ```python title=skeleton ```` 代码块：设 `CN_FONT = "微软雅黑"`、`EN_FONT = "Arial"`，给出 `set_cn_font(style_or_run, cn=CN_FONT, en=EN_FONT)`（设 `font.name` 与 `rPr.rFonts` 的 `w:eastAsia`），对 Normal 与 Heading 1~3 样式调用；加一级标题、一段正文、一个带表头行的 2×3 表格（表头加粗、`table.style = "Table Grid"`）、一张用 matplotlib 生成的小图（`add_picture` 按页面可用宽度的 80%）、页脚页码（`PAGE` 字段）；保存为 `骨架 示例.docx`。代码必须能在沙箱里原样运行（`case_docx_create` 会执行它）。
  4. 「修改已有文件」：`replace_text.py` 与 `fill_template.py` 的完整用法（含 JSON 格式示例、输出字段含义）；两条硬规则（另存新文件、改完走检查）；增删段落 / 插图 / 页眉页脚用 python-docx 的一两行示例。
  5. 「转换」：`convert.py 输入 --to pdf`；`.doc` → `--to docx`。
  6. 「检查成品」三步（spec §4.7 原样）。
  7. 「字体」：spec §4.6 表格里 Word 一行的规则 + 员工指定字体时的提醒话术：「客户电脑没装这个字体时，Word 会自动替换成别的字体。」+ 品牌字体要求严格时建议交付 PDF。
  8. 「常见坑」至少含：只设 `font.name` 中文仍是宋体 → 要设 eastAsia；`paragraph.text = ...` 会冲掉格式 → 改 run 或用 `replace_text.py`；表格列宽要设在每个单元格上才生效；图片过大溢出页面 → 按可用宽度缩放。
  9. 「做不到」：修订痕迹、批注、改内嵌图表 / SmartArt 数据、宏 —— 如实告诉用户做不到。

- [ ] **Step 6: 跑第二层 docx 用例与第一层全部测试**

Run: `uv run --no-sync pytest platform-skills/tests -q -k "docx or build"`（带环境变量）
Expected: PASS

- [ ] **Step 7: 自证** —— 把 `replace_in_paragraph` 里 `(replace if i == first else "")` 改成 `replace`（每个被覆盖的 run 都写替换文本），`case_docx_replace` 必须红；还原并确认。

- [ ] **Step 8: Commit**

```bash
git add platform-skills/docx platform-skills/tests/in_image/case_docx_*.py
git commit -m "feat(platform-skills): docx 技能(新建骨架 + 保留格式替换 + 模板填充)"
```

---

### Task 4: `pptx` 技能

**Files:**
- Create: `platform-skills/pptx/SKILL.md`, `skill.yaml`, `scripts/inspect_template.py`, `scripts/duplicate_slide.py`
- Create: `platform-skills/tests/in_image/case_pptx_create.py`, `case_pptx_duplicate.py`, `case_pptx_inspect.py`

**Interfaces:**
- Consumes: Task 2 共享脚本与 `_harness`。
- Produces:
  - `skill.yaml`：`shared: [_cli.py, _office.py, convert.py, preview.py]`
  - `inspect_template.py DECK.pptx` → `{"slide_size": [w_emu, h_emu], "layouts": [{"index": int, "name": str, "placeholders": [{"idx": int, "type": str, "name": str, "left": int, "top": int, "width": int, "height": int}]}], "slides": [{"number": int, "layout": str, "shapes": [{"name": str, "type": str, "has_text": bool}]}]}`
  - `duplicate_slide.py IN.pptx OUT.pptx --index N [--after M]` → `{"new_slide_number": int, "shared_parts_warning": [str]}`（页码从 1 开始；不给 `--after` 时放在原页之后）

- [ ] **Step 1: 写第二层用例**

`case_pptx_duplicate.py`:
```python
import io
import json
import os

import pptx
from PIL import Image
from pptx.util import Inches
from _harness import check, run, script

os.chdir("/workspace")
p = pptx.Presentation()
for i in range(3):
    s = p.slides.add_slide(p.slide_layouts[5]); s.shapes.title.text = f"第{i + 1}页"
buf = io.BytesIO(); Image.new("RGB", (60, 40), (200, 30, 30)).save(buf, "PNG"); buf.seek(0)
p.slides[1].shapes.add_picture(buf, Inches(1), Inches(2))
p.save("汇报 原件.pptx")
res = json.loads(run(["python", script("pptx", "duplicate_slide.py"), "汇报 原件.pptx", "汇报 新.pptx", "--index", "2"]).stdout)
check(res["new_slide_number"] == 3, f"res={res}")
out = pptx.Presentation("汇报 新.pptx")
titles = [s.shapes.title.text for s in out.slides]
check(titles == ["第1页", "第2页", "第2页", "第3页"], f"titles={titles}")
pics = [sh for sh in out.slides[2].shapes if sh.shape_type == 13]
check(len(pics) == 1 and pics[0].image.blob, "duplicated slide lost its picture")
out.slides[2].shapes.title.text = "改过的副本"
out.save("汇报 新2.pptx")
check(pptx.Presentation("汇报 新2.pptx").slides[1].shapes.title.text == "第2页", "editing copy changed original")
run(["python", script("pptx", "convert.py"), "汇报 新2.pptx", "--to", "pdf", "--out-dir", "pdf"])
res = json.loads(run(["python", script("pptx", "duplicate_slide.py"), "汇报 原件.pptx", "汇报 首.pptx", "--index", "3", "--after", "0"]).stdout)
check([s.shapes.title.text for s in pptx.Presentation("汇报 首.pptx").slides][0] == "第3页", "--after 0 = first")
run(["python", script("pptx", "duplicate_slide.py"), "汇报 原件.pptx", "x.pptx", "--index", "9"], expect=1)
print("PASS case_pptx_duplicate")
```

`case_pptx_inspect.py`：对 python-pptx 默认模板跑 `inspect_template.py`，断言 `layouts` 数 == 11、第 0 个版式名为 `Title Slide` 且含 `idx` 0 与 1 两个占位符、`slides` 与实际页数一致。

`case_pptx_create.py`：与 docx 同法执行 `SKILL.md` 里 ```` ```python title=skeleton ```` 代码块（必须生成 `骨架 示例.pptx`，16:9，≥ 3 页：封面、带表格的一页、带 matplotlib 图片的一页；东亚字体设为 `微软雅黑`），断言：`prs.slide_width == 12192000 and prs.slide_height == 6858000`；任一文本 run 的 `a:ea` typeface == `微软雅黑`；转 PDF 后 pdfplumber 能提取出骨架里的中文标题；`preview.py` 出图非空白。

- [ ] **Step 2: 跑确认失败**（`-k pptx`）

- [ ] **Step 3: 实现 `inspect_template.py`**（遍历 `prs.slide_layouts` 与 `prs.slides`；`type` 用 `str(ph.placeholder_format.type)`；`shape.shape_type` 为 None 时写 `"unknown"`）。

- [ ] **Step 4: 实现 `duplicate_slide.py`**

```python
"""Duplicate one slide (shapes + its own copies of picture/media relationships)."""

from __future__ import annotations

import argparse
import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pptx  # noqa: E402
from _cli import emit_json, fail, resolve_io  # noqa: E402
from pptx.opc.constants import RELATIONSHIP_TYPE as RT  # noqa: E402

_R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_COPY_RELS = {RT.IMAGE, RT.MEDIA, RT.VIDEO, RT.AUDIO, RT.HYPERLINK}
_SHARED_WARN = {RT.CHART: "图表", RT.DIAGRAM_DATA: "SmartArt", RT.OLE_OBJECT: "嵌入对象", RT.PACKAGE: "嵌入文件"}


def duplicate(prs, index: int, after: int) -> tuple[int, list[str]]:
    src = prs.slides[index - 1]
    new = prs.slides.add_slide(src.slide_layout)
    for shape in list(new.shapes):
        shape._element.getparent().remove(shape._element)
    rid_map: dict[str, str] = {}
    warnings: set[str] = set()
    for rid, rel in src.part.rels.items():
        if rel.reltype == RT.NOTES_SLIDE or rel.reltype == RT.SLIDE_LAYOUT:
            continue
        if rel.is_external:
            rid_map[rid] = new.part.relate_to(rel.target_ref, rel.reltype, is_external=True)
        else:
            rid_map[rid] = new.part.relate_to(rel.target_part, rel.reltype)
        if rel.reltype in _SHARED_WARN:
            warnings.add(_SHARED_WARN[rel.reltype])
    for el in src.shapes._spTree.iterchildren():
        if el.tag.endswith("}nvGrpSpPr") or el.tag.endswith("}grpSpPr"):
            continue
        clone = copy.deepcopy(el)
        for node in clone.iter():
            for attr, val in list(node.attrib.items()):
                if attr.startswith(f"{{{_R_NS}}}") and val in rid_map:
                    node.set(attr, rid_map[val])
        new.shapes._spTree.append(clone)
    ids = prs.slides._sldIdLst
    moved = ids[-1]
    ids.remove(moved)
    ids.insert(after, moved)
    return after + 1, sorted(warnings)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("input")
    ap.add_argument("output")
    ap.add_argument("--index", type=int, required=True, help="要复制的页码（从 1 开始）")
    ap.add_argument("--after", type=int, help="放在第几页之后（0 = 最前）；默认紧跟原页")
    args = ap.parse_args()
    src, dst = resolve_io(args.input, args.output)
    prs = pptx.Presentation(str(src))
    total = len(prs.slides)
    if not 1 <= args.index <= total:
        fail(f"--index 超出范围：共 {total} 页")
    after = args.index if args.after is None else args.after
    if not 0 <= after <= total:
        fail(f"--after 超出范围：0..{total}")
    number, warnings = duplicate(prs, args.index, after)
    prs.save(str(dst))
    emit_json({"new_slide_number": number, "shared_parts_warning": warnings})


if __name__ == "__main__":
    main()
```

> 说明：`_COPY_RELS` 以外的关系（图表、SmartArt、OLE）也会 `relate_to` 到**同一个**目标 part —— 这就是「数据部件共享」的来源，输出里以 `shared_parts_warning` 告知。实现时若 `RT` 常量名与所装 python-pptx 版本不符，以 `python -c "from pptx.opc.constants import RELATIONSHIP_TYPE as RT; print([n for n in dir(RT) if not n.startswith('_')])"` 在镜像里实测为准，并删掉未使用的 `_COPY_RELS` 变量（ruff 会报）。

- [ ] **Step 5: 写 `SKILL.md`**（frontmatter `name: pptx`，description：`新建、修改或转换 PowerPoint 演示文稿（.pptx）时使用：汇报、方案展示、按机构模板出 PPT、转 PDF。只读取内容用 read_document，不用本技能。`）。必含：环境段（逐字，技能名 pptx）；骨架代码块（16:9、四级字号层级常量、`set_cn_font` 设 `a:ea` 与 `a:latin`、封面 / 表格页 / 图片页、`CN_FONT = "微软雅黑"`，保存 `骨架 示例.pptx`）；版式网格与留白原则（统一边距、同级元素对齐、一页一个视觉焦点、内容多就拆页不缩字号）；套模板流程（先 `inspect_template.py` 看版式与占位符 → `prs.slide_layouts[i]` 加页 → 按 `placeholders[idx]` 填）；删页调序（`prs.slides._sldIdLst` 的 remove / insert 一两行示例，说明删页后要保存另存）；复制一页用 `duplicate_slide.py` 及共享部件告警含义；嵌视频 `add_movie`（给封面图）；转换；检查三步；字体规则（PPT 行）；常见坑（`text_frame.text = ...` 冲掉格式；原生图表在 LibreOffice 预览里可能与 PowerPoint 不同 → 推荐 matplotlib 出图；图片不按比例缩放会变形）；做不到（动画、改内嵌图表数据、SmartArt、宏）。

- [ ] **Step 6: 跑 `-k "pptx or build"` 全绿；自证** —— 删掉 `duplicate()` 里改写 `r:` 属性的循环，`case_pptx_duplicate` 必须因图片丢失或文件损坏变红；还原。

- [ ] **Step 7: Commit**

```bash
git add platform-skills/pptx platform-skills/tests/in_image/case_pptx_*.py
git commit -m "feat(platform-skills): pptx 技能(新建骨架 + 模板检视 + 复制一页)"
```

---

### Task 5: `xlsx` 技能

**Files:**
- Create: `platform-skills/xlsx/SKILL.md`, `skill.yaml`, `scripts/recalc.py`
- Create: `platform-skills/tests/in_image/case_xlsx_create.py`, `case_xlsx_recalc.py`

**Interfaces:**
- Consumes: `_office.run_soffice(..., recalc=True)`、`_office.convert(..., recalc=True)`、`_cli`。
- Produces:
  - `skill.yaml`：`shared: [_cli.py, _office.py, convert.py, preview.py]`
  - `recalc.py IN.xlsx OUT.xlsx [--timeout 120]` → `{"formulas": int, "errors": [{"sheet": str, "cell": str, "value": str}]}`；有错误退出码 2，否则 0。

- [ ] **Step 1: 用例**

`case_xlsx_recalc.py`:
```python
import json
import os

import openpyxl
from _harness import check, run, script

os.chdir("/workspace")
wb = openpyxl.Workbook(); ws = wb.active; ws.title = "数据"
ws["A1"], ws["A2"], ws["A3"] = 2, 3, "=A1*A2"
ws2 = wb.create_sheet("错误"); ws2["B2"] = "=1/0"; ws2["B3"] = "=数据!A3+1"
wb.save("预算 表.xlsx")
res = json.loads(run(["python", script("xlsx", "recalc.py"), "预算 表.xlsx", "重算/预算 表.xlsx"], expect=2).stdout)
check(res["formulas"] == 3, f"formulas={res['formulas']}")
check(res["errors"] == [{"sheet": "错误", "cell": "B2", "value": "#DIV/0!"}], f"errors={res['errors']}")
vals = openpyxl.load_workbook("重算/预算 表.xlsx", data_only=True)
check(vals["数据"]["A3"].value == 6 and vals["错误"]["B3"].value == 7, "cached values missing after recalc")
check(openpyxl.load_workbook("重算/预算 表.xlsx")["数据"]["A3"].value == "=A1*A2", "formula lost")
wb2 = openpyxl.Workbook(); wb2.active["A1"] = "=1+1"; wb2.save("ok.xlsx")
run(["python", script("xlsx", "recalc.py"), "ok.xlsx", "ok2.xlsx"], expect=0)
run(["python", script("xlsx", "recalc.py"), "ok.xlsx", "ok.xlsx"], expect=1)
print("PASS case_xlsx_recalc")
```

`case_xlsx_create.py`：执行 SKILL.md 骨架（生成 `骨架 示例.xlsx`：表头加粗填充、列宽、冻结首行、数字 / 百分比格式、一列 `=SUM` 公式、一张 matplotlib 图插入），断言：`ws.freeze_panes == "A2"`、公式单元格值以 `=` 开头、表头字体加粗、`recalc.py` 退出码 0 且重算后 SUM 的缓存值正确、`preview.py` 出图非空白。

- [ ] **Step 2: 确认失败**（`-k xlsx`）

- [ ] **Step 3: 实现 `recalc.py`**

```python
"""Recalculate every formula with LibreOffice and report error cells."""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _office  # noqa: E402
import openpyxl  # noqa: E402
from _cli import emit_json, fail, resolve_io  # noqa: E402

_ERRORS = {"#REF!", "#DIV/0!", "#VALUE!", "#NAME?", "#N/A", "#NUM!", "#NULL!"}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("input")
    ap.add_argument("output")
    ap.add_argument("--timeout", type=float, default=120.0)
    args = ap.parse_args()
    src, dst = resolve_io(args.input, args.output)
    tmp = Path(tempfile.mkdtemp(prefix="recalc-"))
    try:
        staged = tmp / "in" / f"book{src.suffix.lower()}"
        staged.parent.mkdir()
        shutil.copyfile(src, staged)
        try:
            out = _office.convert(staged, "xlsx", tmp / "out", timeout=args.timeout, recalc=True) \
                if src.suffix.lower() != ".xlsx" else _recalc_same_format(staged, tmp / "out", args.timeout)
        except (_office.OfficeError, _office.OfficeTimeout) as exc:
            fail(str(exc))
        shutil.copyfile(out, dst)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    formulas_wb = openpyxl.load_workbook(dst)
    values_wb = openpyxl.load_workbook(dst, data_only=True)
    formulas, errors = 0, []
    for ws in formulas_wb.worksheets:
        vws = values_wb[ws.title]
        for row in ws.iter_rows():
            for cell in row:
                if isinstance(cell.value, str) and cell.value.startswith("="):
                    formulas += 1
                v = vws[cell.coordinate].value
                if isinstance(v, str) and v in _ERRORS:
                    errors.append({"sheet": ws.title, "cell": cell.coordinate, "value": v})
    emit_json({"formulas": formulas, "errors": errors})
    sys.exit(2 if errors else 0)


def _recalc_same_format(src: Path, out_dir: Path, timeout: float) -> Path:
    """xlsx -> xlsx: LibreOffice refuses same-format --convert-to; round-trip via the Calc filter name."""
    out_dir.mkdir(parents=True, exist_ok=True)
    proc = _office.run_soffice(
        ["--convert-to", 'xlsx:"Calc MS Excel 2007 XML"', "--outdir", str(out_dir), str(src)],
        timeout=timeout, recalc=True,
    )
    target = out_dir / f"{src.stem}.xlsx"
    if proc.returncode != 0 or not target.is_file():
        raise _office.OfficeError(f"重算失败（退出码 {proc.returncode}）：{proc.stderr.strip()[-500:]}")
    return target


if __name__ == "__main__":
    main()
```

> 实现时在镜像里实测：(a) `--convert-to` 的 filter 参数写法（带不带引号）哪种被接受；(b) `OOXMLRecalcMode=0` 是否确实让无缓存值的公式得到计算结果（`case_xlsx_recalc` 断言 `A3 == 6` 就是这条的验证）。若 (b) 不成立，改用 soffice 的 Basic 宏 `calculateAll()` 方案，并把实测过程写进脚本 docstring。**不得**在未验证时宣称通过。

- [ ] **Step 4: `SKILL.md`**（`name: xlsx`，description：`新建、修改或转换 Excel 表格（.xlsx）时使用：数据表、预算、清单、带公式的报表、转 PDF。只读取内容用 read_document，不用本技能。`）。必含：环境段（逐字）；骨架（见 Step 1）；「公式写成公式」原则；数字格式示例；修改已有表时「原文件里的图表 / 图片保存后会丢失 → 先检查 `ws._charts` / `ws._images`，有就先告诉用户」；`recalc.py` 用法与退出码含义（2 = 有错误单元格，按输出逐个修）；转换；检查三步（xlsx 额外先跑 recalc）；字体（xlsx 同 Word 规则，单元格字体 `Font(name="微软雅黑")`）；常见坑（openpyxl 写的公式没有缓存值，别的软件打开前要 recalc；合并单元格只能写左上角；日期要设 number_format）；做不到（数据透视表、宏、保留原有图表）。

- [ ] **Step 5: 跑 `-k "xlsx or build"`；自证** —— 把 `_office` 的 `recalc=True` 分支写 xcu 的代码注释掉，`case_xlsx_recalc` 的缓存值断言必须变红（这同时证明重算设置确实生效）；还原。

- [ ] **Step 6: Commit** `feat(platform-skills): xlsx 技能(新建骨架 + LibreOffice 重算与错误单元格报告)`

---

### Task 6: `pdf` 技能

**Files:**
- Create: `platform-skills/pdf/SKILL.md`, `skill.yaml`, `scripts/pdf_ops.py`
- Create: `platform-skills/tests/in_image/case_pdf_create.py`, `case_pdf_ops.py`

**Interfaces:**
- Consumes: `_cli`、`preview.py`（pdf 只声明 `shared: [_cli.py, _office.py, preview.py]` —— `preview.py` 依赖 `_office` 模块导入，即使输入是 pdf 用不到 soffice）。
- Produces：`pdf_ops.py` 子命令（输出一律新文件，JSON 一行）：
  - `info IN.pdf` → `{"pages": int, "sizes_pt": [[w, h], ...], "encrypted": bool, "has_text": bool}`
  - `merge OUT.pdf IN1.pdf IN2.pdf ...` → `{"output": str, "pages": int}`
  - `split IN.pdf OUT_DIR [--pages SPEC]` → `{"outputs": [str]}`（无 `--pages`：每页一个文件 `<stem>-p001.pdf`；有：按 SPEC 抽成一个文件 `<stem>-pages.pdf`）
  - `rotate IN.pdf OUT.pdf --degrees 90|180|270 [--pages SPEC]` → `{"output": str, "rotated": [int]}`
  - `watermark IN.pdf OUT.pdf --text TEXT [--opacity 0.15] [--angle 45] [--font "Noto Sans CJK SC"]` → `{"output": str}`
  - `stamp IN.pdf OUT.pdf --stamp STAMP.pdf [--pages SPEC]` → `{"output": str, "stamped": [int]}`
  - 页码规格与 `preview.py` 同语法（`1-3,5`、`all`）。

- [ ] **Step 1: 用例**

`case_pdf_ops.py`:
```python
import json
import os

from pypdf import PdfReader
from weasyprint import HTML
from _harness import check, run, script

os.chdir("/workspace")
HTML(string="<html><body>" + "".join(f"<h1 style='break-before:page'>第{i}页</h1>" for i in range(1, 5)) + "</body></html>").write_pdf("原 文件.pdf")
HTML(string="<p>另一份</p>").write_pdf("附件.pdf")
info = json.loads(run(["python", script("pdf", "pdf_ops.py"), "info", "原 文件.pdf"]).stdout)
check(info["pages"] == 4 and info["has_text"] and not info["encrypted"], f"info={info}")
m = json.loads(run(["python", script("pdf", "pdf_ops.py"), "merge", "合并/合并.pdf", "原 文件.pdf", "附件.pdf"]).stdout)
check(m["pages"] == 5 and len(PdfReader(m["output"]).pages) == 5, f"merge={m}")
s = json.loads(run(["python", script("pdf", "pdf_ops.py"), "split", "原 文件.pdf", "拆分"]).stdout)
check(len(s["outputs"]) == 4, f"split={s}")
s2 = json.loads(run(["python", script("pdf", "pdf_ops.py"), "split", "原 文件.pdf", "拆分2", "--pages", "2-3"]).stdout)
check(len(s2["outputs"]) == 1 and len(PdfReader(s2["outputs"][0]).pages) == 2, f"split pages={s2}")
r = json.loads(run(["python", script("pdf", "pdf_ops.py"), "rotate", "原 文件.pdf", "转.pdf", "--degrees", "90", "--pages", "2"]).stdout)
rd = PdfReader("转.pdf")
check(rd.pages[1].rotation == 90 and rd.pages[0].rotation == 0, f"rotate={r}")
run(["python", script("pdf", "pdf_ops.py"), "watermark", "原 文件.pdf", "水印.pdf", "--text", "内部资料 勿外传"])
wm = PdfReader("水印.pdf")
check(len(wm.pages) == 4 and "内部资料" in (wm.pages[2].extract_text() or ""), "watermark text missing on page 3")
fonts = str(wm.pages[0]["/Resources"])
check("CJK" in fonts or "Noto" in fonts, f"watermark font not embedded CJK: {fonts[:300]}")
HTML(string="<p style='color:red;font-size:40pt'>已审核</p>").write_pdf("章.pdf")
st = json.loads(run(["python", script("pdf", "pdf_ops.py"), "stamp", "原 文件.pdf", "盖章.pdf", "--stamp", "章.pdf", "--pages", "4"]).stdout)
check(st["stamped"] == [4] and "已审核" in (PdfReader("盖章.pdf").pages[3].extract_text() or ""), f"stamp={st}")
run(["python", script("pdf", "pdf_ops.py"), "rotate", "原 文件.pdf", "原 文件.pdf", "--degrees", "90"], expect=1)

# preview.py and pdf_ops.py each carry a copy of _parse_pages — the two must agree.
import importlib.util
import sys
sys.path.insert(0, os.path.dirname(script("pdf", "pdf_ops.py")))


def _load(path):
    spec = importlib.util.spec_from_file_location(os.path.basename(path)[:-3] + "_cmp", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


prev, ops = _load(script("pdf", "preview.py")), _load(script("pdf", "pdf_ops.py"))
for spec_text, total in [("1-3", 5), ("2,4", 5), ("all", 3), ("4-9", 5), ("1,1,2", 2)]:
    check(prev._parse_pages(spec_text, total) == ops._parse_pages(spec_text, total), f"page spec disagree: {spec_text}")
print("PASS case_pdf_ops")
```

`case_pdf_create.py`：执行 SKILL.md 骨架（HTML + CSS → weasyprint，`@page` A4 与页边距、页眉文字、页脚 `counter(page)` / `counter(pages)`、中文标题与表格、`break-inside: avoid`、matplotlib 图片；生成 `骨架 示例.pdf`，≥ 2 页），断言：页数 ≥ 2；第 1 页字体资源里含 `NotoSansCJK`（嵌入）；pdfplumber 提取文本含骨架中文标题与页脚页码（如 `1 / 2`）；`preview.py` 出图非空白。

- [ ] **Step 2: 确认失败**（`-k pdf`）

- [ ] **Step 3: 实现 `pdf_ops.py`**（argparse 子命令；共用 `_parse_pages(spec, total)`，与 `preview.py` 同语义 —— 从 `preview` 导入会引入 `_office`，所以在 `pdf_ops.py` 内复制这个 12 行函数并在两处 docstring 互相注明「语义须一致」，`case_pdf_ops.py` 末尾断言两者对同一组输入结果相同）。关键实现：
  - `watermark`：对每个页面尺寸用 weasyprint 生成一页同尺寸 PDF：
    ```python
    html = f"""<html><head><style>
    @page {{ size: {w}pt {h}pt; margin: 0 }}
    body {{ margin:0; width:{w}pt; height:{h}pt; display:flex; align-items:center; justify-content:center }}
    div {{ font-family:"{font}"; font-size:{min(w, h) / 10:.0f}pt; color: rgba(128,128,128,{opacity});
           transform: rotate(-{angle}deg); white-space: nowrap }}
    </style></head><body><div>{escape(text)}</div></body></html>"""
    ```
    `HTML(string=html).write_pdf()` 得到字节 → `PdfReader(BytesIO(...)).pages[0]` → 原页 `page.merge_page(stamp_page)`；按页面尺寸缓存水印页。
  - `stamp`：`stamp` 文件第 1 页 `merge_page` 到指定页。
  - `rotate`：`page.rotate(deg)`。
  - 加密的输入：`info` 如实报 `encrypted: true`；其它子命令遇到加密文件 → `fail("PDF 已加密，本技能不处理加密文件")`。
  - 所有写出用 `PdfWriter`，`writer.write(dst)`；`dst` 经 `resolve_io` 检查不覆盖原件（`merge` 的每个输入都要与输出比较）。

- [ ] **Step 4: `SKILL.md`**（`name: pdf`，description：`新建 PDF，或对已有 PDF 做合并、拆分、旋转、加水印、盖章时使用。只读取 PDF 内容用 read_document，不用本技能；PDF 里的文字不能直接改，要回到源文件改后重新生成。`）。必含：环境段（逐字，技能名 pdf）；骨架代码块（见 Step 1）；分页控制与页眉页脚 CSS 要点；`pdf_ops.py` 全部子命令用法；「改 PDF 文字 → 回到源文件重新生成」的明确说明；检查三步；字体（PDF 行：沙箱字体、`fc-list :lang=zh` 查询、沒有就如实说用了 Noto Sans CJK 代替、不下载字体）；常见坑（CSS flex / grid 在 weasyprint 里支持有限 → 表格布局用 table；图片用绝对路径或 `base_url`；长表格跨页要 `thead` 重复表头）；做不到（表单填写、加密解密、OCR、改文字）。

- [ ] **Step 5: 跑 `-k "pdf or build"`；自证** —— 把 `watermark` 里的 `merge_page` 注释掉，`case_pdf_ops` 必须红；还原。

- [ ] **Step 6: Commit** `feat(platform-skills): pdf 技能(weasyprint 新建骨架 + 合并拆分旋转水印盖章)`

---

### Task 7: 第一层平台一致性测试（四个技能到齐后）

**Files:**
- Create: `platform-skills/tests/test_platform_checks.py`

**Interfaces:**
- Consumes: `built_packages`、`unpacked_skills`、`EXPECTED_SKILLS`；平台代码：
  - `control_plane.api._skill_zip.parse_skill_zip(blob: bytes, *, asset_tier: bool) -> SkillZipPayload`
  - `control_plane.api._skill_moderation.moderate_prompt_fragment(...)`（签名以源码为准：`services/control-plane/src/control_plane/api/_skill_moderation.py`，调用方式照 `platform_skills.py` 的 `_ingest_platform_skill_payload` 里那一处原样复制）
  - `expert_work.common.threat_patterns.scan_for_threats(text, scope="strict" | "context") -> list`
  - `orchestrator.tools.sandbox_image_contract.SANDBOX_PREINSTALLED_PYTHON` / `SANDBOX_PREINSTALLED_BINARIES`

- [ ] **Step 1: 写测试**

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
from orchestrator.tools import sandbox_image_contract as contract

# Not imported from conftest: with --import-mode=importlib a test module cannot
# reliably ``import conftest``. Keep in sync with conftest.EXPECTED_SKILLS.
EXPECTED_SKILLS = ("docx", "pptx", "xlsx", "pdf")

_JS_ROUTE = re.compile(r"\bnpm\b|require\(|pptxgenjs|docx-js|\bnode\s", re.I)
_SCRIPT_REF = re.compile(r"\$EXPERT_WORK_SKILLS_DIR/([a-z]+)/scripts/([A-Za-z0-9_]+\.py)")
_ENV_BLOCK = re.compile(r"^## 环境\n(.*?)(?=^## )", re.S | re.M)
_LIBS_LINE = re.compile(r"已预装、直接用、不要装：(.*?)；命令行 (.*?)；")


def _frontmatter(md: str) -> dict:
    _, fm, _ = md.split("---\n", 2)
    return yaml.safe_load(fm)


def test_exactly_the_four_office_skills_exist(built_packages):
    assert sorted(built_packages) == sorted(EXPECTED_SKILLS)


@pytest.mark.parametrize("name", EXPECTED_SKILLS)
def test_platform_zip_parse_accepts_package(built_packages, name):
    payload = parse_skill_zip(built_packages[name].read_bytes(), asset_tier=False)
    assert payload.name == name
    assert payload.lazy_load is True
    assert payload.license == "深护智康自研，仅限本平台使用"
    # same three moderation calls, same order, as _ingest_platform_skill_payload
    moderate_prompt_fragment(payload.prompt_fragment, lazy_load=payload.lazy_load)
    moderate_tool_names(payload.tool_names)
    moderate_required_models(payload.required_models)


@pytest.mark.parametrize("name", EXPECTED_SKILLS)
def test_threat_scans_pass_body_and_every_text_file(unpacked_skills, name):
    for f in sorted((unpacked_skills / name).rglob("*")):
        if f.is_file():
            text = f.read_text(encoding="utf-8")
            assert not scan_for_threats(text, scope="strict"), f"strict scan hit: {f}"
            assert not scan_for_threats(text, scope="context"), f"context scan hit (would be dropped at seed): {f}"


@pytest.mark.parametrize("name", EXPECTED_SKILLS)
def test_frontmatter(unpacked_skills, name):
    fm = _frontmatter((unpacked_skills / name / "SKILL.md").read_text(encoding="utf-8"))
    assert fm["name"] == name
    assert 0 < len(fm["description"]) <= 200
    assert fm["license"] == "深护智康自研，仅限本平台使用"
    assert fm["expert_work"] == {"lazy": True, "category": "通用"}


@pytest.mark.parametrize("name", EXPECTED_SKILLS)
def test_body_length_and_no_js_route(unpacked_skills, name):
    root = unpacked_skills / name
    body = (root / "SKILL.md").read_text(encoding="utf-8").split("---\n", 2)[2]
    assert len(body) <= 6000, f"{name} body {len(body)} chars > 6000"
    for f in [root / "SKILL.md", *sorted((root / "scripts").glob("*.py"))]:
        assert not _JS_ROUTE.search(f.read_text(encoding="utf-8")), f"JS route in {f}"


@pytest.mark.parametrize("name", EXPECTED_SKILLS)
def test_every_referenced_script_exists_and_every_script_is_referenced(unpacked_skills, name):
    root = unpacked_skills / name
    body = (root / "SKILL.md").read_text(encoding="utf-8")
    refs = _SCRIPT_REF.findall(body)
    assert all(skill == name for skill, _ in refs), f"{name} references another skill's scripts: {refs}"
    referenced = {script for _, script in refs}
    present = {p.name for p in (root / "scripts").glob("*.py")}
    missing = referenced - present
    assert not missing, f"{name}: referenced but not packaged: {missing}"
    helpers = {"_cli.py", "_office.py"}  # imported by other scripts, never called directly
    dead = present - referenced - helpers
    assert not dead, f"{name}: packaged but never mentioned in SKILL.md: {dead}"


def test_env_block_identical_across_skills_modulo_name(unpacked_skills):
    blocks = {}
    for name in EXPECTED_SKILLS:
        m = _ENV_BLOCK.search((unpacked_skills / name / "SKILL.md").read_text(encoding="utf-8"))
        assert m, f"{name}: no '## 环境' section"
        blocks[name] = m.group(1).replace(f"/{name}/", "/<skill>/")
    assert len(set(blocks.values())) == 1, blocks


def test_env_block_claims_match_sandbox_contract(unpacked_skills):
    body = (unpacked_skills / "docx" / "SKILL.md").read_text(encoding="utf-8")
    m = _LIBS_LINE.search(body)
    assert m, "env line format changed"
    libs = {s.strip().lower() for s in m.group(1).split("、")}
    preinstalled = {p.lower() for p in contract.SANDBOX_PREINSTALLED_PYTHON}
    assert libs <= preinstalled, f"claimed but not preinstalled: {libs - preinstalled}"
    bins = {b.command for b in contract.SANDBOX_PREINSTALLED_BINARIES}
    claimed_bins = {w for w in ("soffice", "pdftoppm") if w in m.group(2)}
    assert claimed_bins <= bins

```

> 页码语法一致性（`preview._parse_pages` 与 `pdf_ops._parse_pages`）不在这里测：宿主机 venv 没有 pypdf，导入不了这两个脚本；改在 Task 6 的 `case_pdf_ops.py` 末尾比对。

- [ ] **Step 2: 跑** `uv run --no-sync pytest platform-skills/tests -q` → 全绿（第二层在未设环境变量时 skip）。

- [ ] **Step 3: 自证** —— 在 docx 正文里临时加一行 `npm install docx`，`test_body_length_and_no_js_route[docx]` 必须红；在 pptx 正文里把环境段某个字改掉，`test_env_block_identical...` 必须红；还原。

- [ ] **Step 4: Commit** `test(platform-skills): 第一层平台一致性检查(导入解析/两道威胁扫描/frontmatter/引用完整/环境事实)`

---

### Task 8: 导入脚本 `import_in_pod.py`

**Files:**
- Create: `platform-skills/import_in_pod.py`, `platform-skills/tests/test_import_in_pod.py`
- Modify: `platform-skills/README.md`（发布一节）

**Interfaces:**
- Consumes: 平台代码（pod 内与宿主 venv 都可导入）：
  - `control_plane.api.platform_skills._ingest_platform_skill_payload(*, blob, store, audit, principal, source, origin=None, object_store=None) -> tuple[dict, int]`
  - `control_plane.app._build_sql_stores(settings) -> _SqlStores`（`.skill`、`.audit_log`、`.engine`）、`control_plane.app._build_secret_store(settings, stores)`
  - `control_plane.audit.build_default_audit_logger(store=..., pii_fields_resolver=TenantConfigPiiResolver())`
  - `control_plane.runtime.resolve_object_store_config(...)` + `expert_work.runtime.storage.make_object_store(...)`（与 `app.py:1595-1611` 相同的参数）
  - `control_plane.invalidation_bus.InvalidationBus(redis_client=..., origin=...)`、`InvalidationEvent(kind="platform_skill")`；Redis 连接 `redis.asyncio.from_url(settings.quota_redis_url, encoding="utf-8", decode_responses=True)`（与 `app.py:1362` 相同）
  - `control_plane.tenant_scope.bypass_rls_session`
  - `expert_work.protocol.auth.Principal`
- Produces:
  - 本地：`python platform-skills/import_in_pod.py bundle [--dry-run] PKG.skill ...` → 把「本文件源码 + 嵌入的包（base64）+ 调用语句」打印到 stdout。
  - 用法：`python platform-skills/import_in_pod.py bundle dist/*.skill | kubectl -n expert-work exec -i <pod> -- python3 -`
  - pod 内输出：每个包一行 JSON `{"name", "status": 201|200|"dry-run", "created": bool, "version": int, "content_hash": str}`；有任何 201 → 发一次 `platform_skill` 失效并打印 `{"invalidation": "published"}`；dry-run 不写任何东西、不发失效。
  - 函数 `async def run_import(packages: dict[str, bytes], *, dry_run: bool, deps: ImportDeps) -> list[dict]`，`ImportDeps` 为 dataclass（`store`、`audit`、`object_store`、`publish: Callable[[], Awaitable[None]]`），便于测试注入内存实现。
  - 常量：`PRINCIPAL = Principal(subject_id="platform-skills-import", subject_type="service", tenant_id=UUID(int=0), is_system_admin=True, allowed_tenants="*")`（平台后台任务审计用 `UUID(int=0)` 是既有惯例，见 `skill_curator.py:330`）；`source="platform_skills_repo"`。
  - dry-run 的判断：`parse_skill_zip` + moderation + strict 扫描（照 `_ingest_platform_skill_payload` 的顺序调用同一批函数，**不**调用写入），再用与 ingest 相同的方法算 content_hash，与平台该技能 latest 版本比较，输出 `would_create_version: N+1 | "unchanged"`。

- [ ] **Step 1: 写测试 `test_import_in_pod.py`**（用 `InMemorySkillStore`、`build_default_audit_logger(store=InMemoryAuditLogStore(), pii_fields_resolver=TenantConfigPiiResolver())`、`object_store=None`、`publish` 为计数器）：
  1. 首次导入四个真实包（`built_packages`）→ 每个 `status == 201`、`version == 1`，`publish` 被调用恰好 1 次。
  2. 再导入同样的包 → 全部 `status == 200`、`created is False`，`publish` 调用次数不变。
  3. 改动 docx 正文后重打包再导入 → docx `201`、`version == 2`，其余 `200`。
  4. `dry_run=True` → 不写入（store 里版本数不变）、`publish` 不调用、输出含 `would_create_version`。
  5. `bundle` 子命令生成的程序文本可被 `compile()`，且包含每个包名与 `asyncio.run(`。

- [ ] **Step 2: 跑确认失败** → **Step 3: 实现** → **Step 4: 跑确认通过**

- [ ] **Step 5（控制器执行，子 Agent 不操作集群）: 在测试集群 dry-run 一次（只读，不写）**
  ```bash
  python platform-skills/build.py
  POD=$(KUBECONFIG=~/.kube/expert-work-test.yaml kubectl -n expert-work get pods -l app.kubernetes.io/name=control-plane -o jsonpath='{.items[0].metadata.name}')
  python platform-skills/import_in_pod.py bundle --dry-run platform-skills/dist/*.skill \
    | KUBECONFIG=~/.kube/expert-work-test.yaml kubectl -n expert-work exec -i "$POD" -- python3 -
  ```
  Expected：四行 `"status": "dry-run"`，每行 `would_create_version: 2`（测试环境这四个技能现为第 1 版）。结果贴进任务报告。

- [ ] **Step 6: README 发布一节** 写入上面的 dry-run 与正式导入命令（去掉 `--dry-run`），以及「导入成功会自动发跨副本失效，无需重启」「回滚 = 用 git 历史里的旧源码重新 build + 导入」。

- [ ] **Step 7: Commit** `feat(platform-skills): pod 内导入脚本(复用平台导入管线 + 跨副本失效 + dry-run)`

---

### Task 9: 文档联动

**Files:**
- Modify: `docs/streams/STREAM-OFFICE-DESIGN.md`（OFFICE-ADR-5 追加 2026-09-25 修订；§0.1 / ADR-1 的「运行时卸载 pip」「libreoffice 推后」加更正注）
- Modify: `services/orchestrator/src/orchestrator/tools/sandbox_image_contract.py` 的 `SANDBOX_UNAVAILABLE_NOTE`（删掉「docx/pptx 技能写着 npm」这类针对旧正文的说法，保留「沙箱里没有 npm（node 有）」与 python-docx / python-pptx 的指引；`test_note_states_that_npm_is_gone` 仍须通过）
- Modify: `docs/runbooks/2026-09-24-prod-release-checklist.md`（新增 Step A2「导入 office 技能」：位于 Step A 之后、Step B 之前；含导入前后两条只读核对 SQL、dry-run 命令、正式命令、逐技能 go / no-go 引用、回滚条目；§6 执行记录加一行）
- Modify: `docs/superpowers/ROADMAP.md`（登记本项；旧版本许可风险；新 backlog「沙箱镜像加开源中文字体」）

- [ ] **Step 1: 各处按 spec §9 修改**。执行单 Step A2 的只读核对 SQL：
  ```sql
  SELECT s.name, s.latest_version, v.content_hash
  FROM skill s JOIN skill_version v ON v.skill_id = s.id AND v.version = s.latest_version
  WHERE s.tenant_id IS NULL AND s.name IN ('docx','pptx','xlsx','pdf') ORDER BY s.name;
  ```
  与绑定查询（执行单 §1 已有 `run_sql` 包装可复用）：
  ```sql
  SELECT sk AS skill, count(*) AS agents FROM agent_spec a,
    jsonb_array_elements_text(COALESCE(a.spec_json->'spec'->'skills','[]'::jsonb)) sk
  WHERE a.status <> 'deleted' AND sk IN ('docx','pptx','xlsx','pdf') GROUP BY 1 ORDER BY 1;
  ```
- [ ] **Step 2: 两条 SQL 在测试库用执行单里的 `run_sql` 原样跑一遍**，结果写进任务报告。
- [ ] **Step 3: 跑** `uv run --no-sync pytest services/orchestrator/tests/test_sandbox_preinstalled_manifest.py tools/deploy/test_runbook_pod_commands.py -q` 与 `uv run --no-sync pre-commit run --files <改动的文件>` → 通过。
- [ ] **Step 4: Commit** `docs(platform-skills): ADR-5 修订 + 执行单导入步骤 + ROADMAP + 沙箱 npm 提示去旧`

---

### Task 10（控制器执行，不派子 Agent）：终审 → 合并 → 测试环境验收 → 生产

- [ ] 全分支终审（最强模型），重点：clean-room 约束、Review Focus 五条、第二层用例是否真在只读根 + 断网下跑、导入脚本不会在 dry-run 时写库。
- [ ] 开 PR，CI 全绿（含新 workflow `Platform skills (in sandbox image)`），合并。
- [ ] 测试环境：`build.py` → dry-run → 正式导入 → 检查四行 `201`、`invalidation: published`。
- [ ] 第三层验收（spec §6.3）：临时探针 Agent `office-skills-probe`（对接方租户、glm-5.3、`vision: glm-4.6v`、绑定四个技能），五项任务逐项记录（run_id、是否 `skill_view`、是否运行技能脚本、有无 npm、成品下载后 `preview.py` 目检、`completed`）；`ai-health-plan` 真实出方案一次与改前对比；探针与测试数据删除。
- [ ] 09-27 晚逐技能 go / no-go，结论写进执行单 Step A2。
- [ ] 09-28：按执行单 Step A2 在生产导入（生产操作由用户下令），发布后一次真实 run 验证，填执行单 §6。
- [ ] ROADMAP 销案，更新记忆。
