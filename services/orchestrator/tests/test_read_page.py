"""``read_page`` —— 按需渲染文档单页。"""

from __future__ import annotations

import ast
import contextlib
import io
import json
import os
import shutil
import sys
import zipfile
from pathlib import Path
from uuid import uuid4

import pytest

from orchestrator.multimodal import is_cacheable_image_ref
from orchestrator.tools import read_page
from orchestrator.tools.document_figures import (
    MIN_FIGURE_AREA_RATIO,
    MIN_FIGURE_EDGE_PT,
    RENDERABLE_EXTENSIONS,
    SUPPORTED_EXTENSIONS,
    build_figure_inventory_wrapper,
)
from orchestrator.tools.read_page import (
    _CONVERT_TIMEOUT_S,
    _RENDER_TIMEOUT_S,
    MAX_PAGES_PER_CALL,
    RENDER_DPI,
    ReadPageTool,
    _doc_sha,
    _require_units,
    _validate_rendered_rel,
    build_render_wrapper,
    workspace_figure_ref,
)
from orchestrator.tools.registry import ToolContext
from orchestrator.tools.sandbox import _MAX_EXEC_TIMEOUT_S, RecordingSandboxRuntime, SandboxOutcome
from orchestrator.tools.workspace_paths import WriteToSharedError
from orchestrator.tools.workspace_scope import scoped_path, store_scope


def _ctx(*, agent_key: str = "") -> ToolContext:
    return ToolContext(tenant_id=uuid4(), run_id=uuid4(), user_id=uuid4(), agent_key=agent_key)


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


def test_internal_render_budget_fits_under_the_exec_timeout_cap() -> None:
    """M5 —— 内层预算必须严格小于外层 exec 的超时上限,否则一次合法的满页调用会被
    沙箱供应商自己的兜底超时打断,而不是片段自己的 subprocess timeout 优雅降级。

    回修第 1 轮 I-1 —— 这里原来只算 pptx/pdf 那条路(``90 + 3x30 = 180``),
    Task 4b 给 docx 加了 pdftotext + pdfimages 两个子进程之后它**恒绿**:
    真实最坏值是 ``90 + 90 + 30 + 3x30 = 300``,与 ``_MAX_EXEC_TIMEOUT_S``
    **恰好相等**,不再小于 —— 那条 docstring 写明要防的形状已经发生了,而守它的
    断言看不见。两条路分开算,docx 那条也必须进来。

    这条测试**只对常量做算术**,咬不住"片段到底给每个子进程传了哪一个 timeout"
    —— 把 pdftotext 换回 ``convert_timeout_s`` 时它照样绿(实测变异存活过一次)。
    那一半由 ``test_docx_page_resolvers_use_the_short_timeout`` 从行为上钉。

    边界也说清楚:这里算的是**子进程**预算。片段还有不带超时的纯 Python 工作
    (``_render_sha`` 流式哈希整份文件、``_docx_inventory`` 解析 XML),那部分
    不在这条不变式里 —— 这是既有形状,不是 Task 4b 引入的。
    """
    non_docx = _CONVERT_TIMEOUT_S + MAX_PAGES_PER_CALL * _RENDER_TIMEOUT_S
    assert non_docx < _MAX_EXEC_TIMEOUT_S

    # docx 多两个子进程:整篇 pdftotext 一次 + pdfimages -list 一次。两者都用
    # _RENDER_TIMEOUT_S(它们本来就快),不是 _CONVERT_TIMEOUT_S。
    docx = _CONVERT_TIMEOUT_S + 2 * _RENDER_TIMEOUT_S + MAX_PAGES_PER_CALL * _RENDER_TIMEOUT_S
    assert docx < _MAX_EXEC_TIMEOUT_S, (
        f"docx 支路最坏 {docx}s 没有严格小于 exec 上限 {_MAX_EXEC_TIMEOUT_S}s"
    )


# ---------------------------------------------------------------------------
# 沙箱片段真跑 —— 与 test_document_figures.py:44 同一房规:用 exec(compile(...))
# 真跑生成的片段,而不是在源码字符串里断言子串。这里额外伪造 soffice /
# pdftoppm 到 PATH 最前面,让片段的 subprocess.run 调用真正落到桩程序上。
# ---------------------------------------------------------------------------


#: 两个桩都先跑这一段:把本次是第几次被调用记进 ``<bin>/<binary>_calls``,
#: 供需要"第一次成功、第二次失败"这类剧本的 body 用 ``count`` 分支。
def _stub_source(*, binary: str, preamble: str, body: str) -> str:
    return (
        (
            # 绝对解释器路径,不走 ``env python3``(回修第 2 轮 Minor-3):有测试会把
            # PATH 收窄到只剩桩目录来模拟"沙箱里没装某个工具",那时 ``env`` 连
            # python3 都找不到,桩会以非零退出,红出一个与被测行为无关的原因。
            f"#!{sys.executable}\n"
            "import os, sys\n"
            "\n"
            "args = sys.argv[1:]\n"
            "_counter = os.path.join(os.path.dirname(os.path.abspath(__file__)), "
            + repr(f"{binary}_calls")
            + ")\n"
            "count = 0\n"
            "if os.path.exists(_counter):\n"
            "    count = int(open(_counter).read().strip() or '0')\n"
            "count += 1\n"
            "with open(_counter, 'w') as fh:\n"
            "    fh.write(str(count))\n"
        )
        + preamble
        + body
    )


#: soffice body 可用的变量:``args`` ``outdir`` ``src`` ``base`` ``name``
#: ``out_pdf`` ``count``。
_SOFFICE_PREAMBLE = (
    'outdir = args[args.index("--outdir") + 1]\n'
    "src = args[-1]\n"
    "os.makedirs(outdir, exist_ok=True)\n"
    "base = os.path.basename(src)\n"
    "name = os.path.splitext(base)[0]\n"
    'out_pdf = os.path.join(outdir, name + ".pdf")\n'
)
#: pdftoppm body 可用的变量:``args`` ``unit`` ``pdf_path`` ``prefix``
#: ``pdf_content`` ``count``。
_PDFTOPPM_PREAMBLE = (
    'unit = int(args[args.index("-f") + 1])\n'
    "pdf_path, prefix = args[-2], args[-1]\n"
    "with open(pdf_path) as fh:\n"
    "    pdf_content = fh.read()\n"
)

#: 默认 soffice:把源文件的完整 basename(含扩展名)写进产出的 pdf 内容里——
#: 两次转换共用同一个 outdir 时后一次会覆盖前一次,读到的内容会变成"另一个源
#: 文件"的标记,借此检出静默覆盖(spec §13.1 #5)。
_SOFFICE_WRITES_A_PDF = 'with open(out_pdf, "w") as fh:\n    fh.write("PDF-FROM:" + base)\n'
#: 默认 pdftoppm:固定按 2 位宽度补零产出(第 3 页 -> page-03.jpg),模拟真实
#: pdftoppm 按总页数补零的行为;把读到的 pdf 内容原样带进 jpg,让上面那条覆盖
#: 检测能一路传到最终产物。
_PDFTOPPM_WRITES_A_JPEG = (
    'with open(f"{prefix}-{unit:02d}.jpg", "w") as fh:\n'
    '    fh.write(f"JPEG-PAGE-{unit}::{pdf_content}")\n'
)


#: Task 4b —— docx 多用两件 poppler 工具把段落序号解析成页号。默认剧本:
#: 两页正文 + 第 1 页一张够大的位图,让"不关心 docx 解析"的既有测试照常过。
_ANCHOR_ONE = "Anchor paragraph one tail sentence."
_ANCHOR_TWO = "Anchor paragraph two tail sentence."

#: ``pdfimages -list`` 的真实表头 —— 逐字取自 2026-09-23 对 poppler 21.11.0 的
#: 实测(两行:列名 + 横线)。桩必须长得和真家伙一样,否则测的是我对格式的
#: 想象而不是解析器。
_PDFIMAGES_HEADER = (
    "page   num  type   width height color comp bpc  enc interp  object ID x-ppi y-ppi size ratio\n"
    + "-" * 92
    + "\n"
)


def _pdftotext_pages(*pages: str) -> str:
    """pdftotext 桩 body:整篇一次输出,页间换页符,**最后一页后面也有一个**
    —— 与真 pdftotext 一致(同一次实测)。"""
    payload = "".join(page + chr(12) for page in pages)
    return "sys.stdout.write(" + repr(payload) + ")\n"


def _pdfimages_line(i: int, row: tuple[int, ...]) -> str:
    """一行 ``pdfimages -list`` 输出。``row`` 是 ``(页号, 宽px, 高px, x-ppi, y-ppi)``,
    可选第 6 项为 ``True`` 时模拟**内联图像**(``BI … ID … EI``):``[inline]`` 顶掉
    ``object`` + ``ID`` 两列,整行因此只有 **15** 个 token 而不是 16
    ——2026-09-23 对 poppler 21.11.0 的实测形状,逐列对齐。"""
    page, w, h, x_ppi, y_ppi = row[:5]
    if len(row) > 5 and row[5]:
        return (
            f"{page:6d} {i:5d} image {w:7d} {h:5d}  gray    1   8  image  no   [inline] "
            f"{x_ppi:5d} {y_ppi:5d}    0B 0.0%"
        )
    return (
        f"{page:6d} {i:5d} image {w:7d} {h:5d}  gray    1   8  image  no        99  0 "
        f"{x_ppi:5d} {y_ppi:5d}   64B 100%"
    )


def _pdfimages_rows(rows: list[tuple[int, ...]]) -> str:
    """pdfimages -list 桩 body —— 显示尺寸由 px/ppi 反推,装饰图与真图靠这两个数分开。"""
    body = "".join(_pdfimages_line(i, row) + "\n" for i, row in enumerate(rows))
    return "sys.stdout.write(" + repr(_PDFIMAGES_HEADER + body) + ")\n"


#: 一张够大的真图(800x600 px @ 200 ppi ≈ 288x216 pt,远在装饰图阈值之上)。
_BIG_ROW = (800, 600, 200, 200)
#: 一张页眉 logo(200x200 px @ 500 ppi ≈ 28.8 pt,短边低于阈值 = 装饰图)。
#: **方的** —— 短边等于长边,所以它分不出"按短边判"和"按长边判"。
_LOGO_ROW = (200, 200, 500, 500)
#: 一条装饰色带(1200x20 px @ 144 ppi ≈ 602x10 pt):短边 10pt 低于阈值 = 装饰图,
#: 但长边 602pt 远在阈值之上。清单侧为这条形状立过
#: ``test_thin_strip_is_skipped_by_the_edge_clause``,渲染侧照着立一条
#: (回修第 1 轮 I-2/N1:`min` 写成 `max` 时只有非方形的行分得出来)。
_STRIP_ROW = (1200, 20, 144, 144)
#: 抬头 banner(3000x400 px @ 480 ppi ≈ 450x60 pt):短边 60pt **高于**阈值,
#: 所以尺寸滤子**滤不掉它** —— 红头文件的常见尺寸(回修第 1 轮 C-1 场景 A)。
_BANNER_ROW = (3000, 400, 480, 480)


def _install_office_stubs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    soffice_body: str = _SOFFICE_WRITES_A_PDF,
    pdftoppm_body: str = _PDFTOPPM_WRITES_A_JPEG,
    pdftotext_body: str | None = None,
    pdfimages_body: str | None = None,
) -> Path:
    """伪造 soffice / pdftoppm / pdftotext / pdfimages 到 PATH 最前面,只让剧本
    那一段随测试变。返回放桩的目录(调用计数文件也在那里)。

    回修第 4 轮 New-M6 —— 这里原先是五个 helper 各自内联一整份 soffice +
    pdftoppm 桩源码(其中两份 pdftoppm 逐字相同),不影响正确性,但下次改桩
    要改五处。收口成这一个入口之后,变的只有 ``*_body``,两段 preamble 与
    调用计数只有一份。

    Task 4b —— 后两件是 docx 的段落序号 -> 页号解析用的。它们也**必须**是桩:
    真机上有没有 poppler 是环境的事,测试不能跟着环境一次绿一次红。
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    bodies = {
        "soffice": (_SOFFICE_PREAMBLE, soffice_body),
        "pdftoppm": (_PDFTOPPM_PREAMBLE, pdftoppm_body),
        "pdftotext": (
            "",
            pdftotext_body
            if pdftotext_body is not None
            else _pdftotext_pages(_ANCHOR_ONE, _ANCHOR_TWO),
        ),
        "pdfimages": (
            "",
            pdfimages_body if pdfimages_body is not None else _pdfimages_rows([(1, *_BIG_ROW)]),
        ),
    }
    for binary, (preamble, body) in bodies.items():
        script = bin_dir / binary
        script.write_text(_stub_source(binary=binary, preamble=preamble, body=body))
        script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    return bin_dir


# ---------------------------------------------------------------------------
# docx 夹具 —— 只塞 ``word/document.xml`` 的最小 OOXML。
#
# ``_docx_inventory`` 只读这一个部件(见 ``document_figures``
# ``_DOCX_INVENTORY_FRAGMENT``),媒体/关系部件对段落序号与锚点没有任何影响,
# 所以不造。``python-docx`` 不在 ``uv.lock`` 里,用它写的测试会在 CI 里**静默
# skip** 而本地通过 —— 本波第 1 轮为此栽过一次,这里一律 stdlib ``zipfile``。
# ---------------------------------------------------------------------------

_DOCX_XML_NS = (
    'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
    'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
)
_PICTURE_URI = "http://schemas.openxmlformats.org/drawingml/2006/picture"
#: 288x192 pt —— 过得了装饰图阈值。
_BIG_EMU = (3657600, 2438400)
#: 28.8x28.8 pt —— 撞装饰图阈值,清单会跳过它。
_LOGO_EMU = (365760, 365760)


def _para(text: str) -> str:
    return f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"


def _figure(emu: tuple[int, int] = _BIG_EMU) -> str:
    cx, cy = emu
    return (
        "<w:p><w:r><w:drawing>"
        f'<wp:inline><wp:extent cx="{cx}" cy="{cy}"/>'
        '<wp:docPr id="1" name="Picture 1"/>'
        f'<a:graphic><a:graphicData uri="{_PICTURE_URI}"/></a:graphic>'
        "</wp:inline></w:drawing></w:r></w:p>"
    )


def _write_docx(path: Path, *body: str) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(
            "word/document.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f"<w:document {_DOCX_XML_NS}><w:body>{''.join(body)}"
            '<w:sectPr><w:pgSz w:w="12240" w:h="15840"/></w:sectPr>'
            "</w:body></w:document>",
        )


def _run_render(
    tmp_path: Path,
    rel: str,
    *,
    units: list[int],
    out_rel: str,
    dpi: int = RENDER_DPI,
    convert_timeout_s: int = _CONVERT_TIMEOUT_S,
    render_timeout_s: int = _RENDER_TIMEOUT_S,
) -> dict:
    """本地真跑渲染片段(与 test_document_figures._run 同一房规)。

    两个 timeout 可覆盖(回修第 1 轮 I-1)—— 把它们拉开成两个**差得很远**的值,
    再让某个桩故意慢,就能从行为上看出片段到底用了哪一个,而不是只对常量做算术。
    """
    code = build_render_wrapper(
        rel,
        units=units,
        ws=str(tmp_path),
        out_rel=out_rel,
        dpi=dpi,
        convert_timeout_s=convert_timeout_s,
        render_timeout_s=render_timeout_s,
    )
    ns: dict = {}
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        exec(compile(code, "<snippet>", "exec"), ns)  # noqa: S102
    return json.loads(buf.getvalue())


def _snippet_params(code: str) -> dict:
    """从片段源码里取回宿主喂进去的那份参数(第一行就是 ``_PARAMS = '<json>'``)。

    回修第 5 轮 I-1 —— 凡是要用「宿主真正拼出来的 out_rel」的测试,都必须从这里
    取,不许自己再拼一份:自己拼的那份就是第二份路径真源,而这一轮整件事就是在
    治这个。
    """
    first_line = code.split("\n", 1)[0]
    return json.loads(ast.literal_eval(first_line.split(" = ", 1)[1]))


async def _real_out_rel(*, path: str = "d.pptx", ctx: ToolContext | None = None) -> str:
    """真跑一次 ``ReadPageTool.call``,把它实际传给片段的 ``out_rel`` 取出来。"""
    runtime = RecordingSandboxRuntime(
        SandboxOutcome(
            stdout=json.dumps({"ok": True, "rendered": []}), stderr="", exit_code=0, timed_out=False
        )
    )
    await ReadPageTool(client=runtime).call({"path": path, "units": [3]}, ctx=ctx or _ctx())
    return str(_snippet_params(runtime.execs[0][1])["out_rel"])


def test_wrapper_uses_a_private_outdir_per_conversion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """spec §13.1 #5 回归钉:report.docx 与 report.pptx 同名不同扩展名,各自的
    中间 pdf 必须落在各自的 out_dir 下 —— 真跑桩程序后数盘面上有几份 pdf:
    私有 outdir 时两份都幸存;共用一个 outdir 时后一份会覆盖前一份,只剩一份。
    """
    _install_office_stubs(tmp_path, monkeypatch)
    (tmp_path / "uploads").mkdir()
    # Task 4b —— docx 这一侧现在会真的被解析:它必须是一份能读的 OOXML,
    # 请求的 unit 也得是清单里真有的那个编号(图在第 2 段,所以是 2)。
    # 这条测试本身管的仍然是 soffice 的同名 outdir 互相覆盖,不是 docx 解析。
    _write_docx(tmp_path / "uploads" / "report.docx", _para(_ANCHOR_ONE), _figure())
    (tmp_path / "uploads" / "report.pptx").write_text("pptx source")

    env_a = _run_render(
        tmp_path, "uploads/report.docx", units=[2], out_rel=".tool_results/r1/figures/aaa"
    )
    env_b = _run_render(
        tmp_path, "uploads/report.pptx", units=[1], out_rel=".tool_results/r1/figures/bbb"
    )
    assert env_a["ok"] is True
    assert env_b["ok"] is True

    pdfs = sorted(tmp_path.glob("**/*.pdf"))
    assert len(pdfs) == 2, f"两份源文档的中间 pdf 应该各自幸存,实际:{pdfs}"
    contents = {p.read_text() for p in pdfs}
    assert contents == {"PDF-FROM:report.docx", "PDF-FROM:report.pptx"}


def test_wrapper_globs_instead_of_building_the_page_filename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """spec §13.1 #6 回归钉:pdftoppm 按总页数补零,第 3 页出 page-03.jpg 而不是
    page-3.jpg —— 真跑桩程序(固定 2 位补零),拼文件名的实现会找不到文件。
    """
    _install_office_stubs(tmp_path, monkeypatch)
    (tmp_path / "d.pdf").write_text("fake pdf bytes")  # 已是 pdf,跳过 soffice 转换

    env = _run_render(tmp_path, "d.pdf", units=[3], out_rel=".tool_results/r1/figures/s")

    assert env["ok"] is True
    rel = env["rendered"][0]["rel"]
    assert rel.endswith("page-03.jpg")
    content = (tmp_path / rel).read_text()
    assert "JPEG-PAGE-3::" in content


def test_wrapper_reports_not_found_for_a_missing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回修 I3 —— 片段真跑:路径打错字与 LibreOffice 坏了不能长得一样。
    ``_resolve`` 用 realpath 确认路径没越权,但不确认文件真的存在;真正的
    存在性检查必须在尝试转换之前做,不然拿一个不存在的文件去跑 soffice 会
    得到含糊的 convert_failed,而不是直白的 not_found。"""
    _install_office_stubs(tmp_path, monkeypatch)
    env = _run_render(tmp_path, "missing.pdf", units=[1], out_rel=".tool_results/r1/figures/s")
    assert env == {"ok": False, "error": "not_found"}


def test_wrapper_does_not_confuse_units_across_calls_sharing_an_out_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回修 C2 —— ``out_dir`` 按 ``(run_id, doc_sha)`` 键、跨调用累积:第一次
    调用渲第 21 页,第二次调用(同一份文档、同一个 out_dir)只请求第 1 页。
    不带 unit 边界的兜底 glob(``prefix + "-*" + "1" + ".jpg"``)会把上一次
    留下的 ``page-21.jpg`` 也匹配上(它以 "1.jpg" 结尾),``sorted(...)[-1]``
    取到的是它不是 ``page-01.jpg``。"""
    _install_office_stubs(tmp_path, monkeypatch)
    (tmp_path / "d.pdf").write_text("fake pdf bytes")
    out_rel = ".tool_results/r1/figures/shared"

    first = _run_render(tmp_path, "d.pdf", units=[21], out_rel=out_rel)
    assert first["ok"] is True
    assert first["rendered"][0]["rel"].endswith("page-21.jpg")

    second = _run_render(tmp_path, "d.pdf", units=[1], out_rel=out_rel)
    assert second["ok"] is True
    assert second["rendered"][0]["rel"].endswith("page-01.jpg"), (
        f"expected page 1, got {second['rendered'][0]['rel']!r} (page-21 leaked across calls)"
    )


#: soffice 桩:把**源文件的内容**原样带进产出的 pdf(默认那份带的是 basename)。
#: 同一条路径换了内容时,产物内容随之变化,New-I1 那条回归才看得出"模型到底
#: 读到了哪一版"。
#: pdftoppm 桩:把收到的 ``-r <dpi>`` 写进产出内容 —— 同一份源文件不同 dpi 产出
#: 不同字节,M-1 那条回归才看得出 rel 有没有跟着分开。
_PDFTOPPM_STAMPS_ITS_DPI = (
    'dpi = args[args.index("-r") + 1]\n'
    'with open(f"{prefix}-{unit:02d}.jpg", "w") as fh:\n'
    '    fh.write(f"JPEG-AT-DPI-{dpi}")\n'
)

_SOFFICE_COPIES_THE_SOURCE_CONTENT = (
    "with open(src) as fh:\n"
    "    source = fh.read()\n"
    'with open(out_pdf, "w") as fh:\n'
    '    fh.write("PDF-FROM:" + source)\n'
)


@pytest.mark.anyio
async def test_a_document_overwritten_between_calls_gets_a_different_rel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回修第 4 轮 New-I1 —— 产出路径必须跟着**文件内容**走。

    原来 ``out_rel`` 只按 ``(run_id, 文档路径)`` 算,同一条路径换了内容拿到的
    是逐字相同的 rel。``is_cacheable_image_ref`` 又对 ``.tool_results/`` 下的
    ref 判 ``True``,而 ``CachingImageResolver`` 是进程级、无 TTL、无失效通道
    的:于是"第一次渲 VERSION-1 → 模型 write_file 覆盖成 VERSION-2 → 再渲一次
    同一页"这条链上,工具说"已渲染 d.pptx 第 3 页",模型看到的却是旧文档的第
    3 页。修法是把内容哈希拼进路径:内容一变 rel 就变,缓存与磁盘同时自然
    失效。
    """
    _install_office_stubs(tmp_path, monkeypatch, soffice_body=_SOFFICE_COPIES_THE_SOURCE_CONTENT)
    (tmp_path / "d.pptx").write_text("VERSION-1")
    # 用宿主真正拼出来的 out_rel(回修第 5 轮 I-1)—— 原来这里写的是
    # ".tool_results/r1/figures/abc",在 is_cacheable_image_ref 还认目录前缀时
    # 看不出问题;判据收窄成认路径形状之后,这条假前缀立刻暴露成假的。
    out_rel = await _real_out_rel()

    first = _run_render(tmp_path, "d.pptx", units=[3], out_rel=out_rel)
    assert first["ok"] is True
    first_rel = first["rendered"][0]["rel"]
    assert (tmp_path / first_rel).read_text() == "JPEG-PAGE-3::PDF-FROM:VERSION-1"

    (tmp_path / "d.pptx").write_text("VERSION-2")
    second = _run_render(tmp_path, "d.pptx", units=[3], out_rel=out_rel)
    assert second["ok"] is True
    second_rel = second["rendered"][0]["rel"]
    assert (tmp_path / second_rel).read_text() == "JPEG-PAGE-3::PDF-FROM:VERSION-2"

    assert second_rel != first_rel, (
        f"文档内容换了,产出 rel 却一模一样({second_rel!r})—— 缓存会继续端出旧字节"
    )
    # 旧 rel 的字节原地还在、没有被新一版原地覆盖:缓存里那条老条目指的依然是
    # 它当初渲的那一版,这正是"内容一变 rel 就变"让缓存自然失效的样子。
    assert (tmp_path / first_rel).read_text() == "JPEG-PAGE-3::PDF-FROM:VERSION-1"

    # 两条都仍然落在可缓存子树里 —— 修法靠的是"缓存键变了",不是把渲染页
    # 整个踢出缓存(那会把每一轮都退化成一次 NAS 读)。
    tenant_id, user_id = uuid4(), uuid4()
    first_ref = workspace_figure_ref(tenant_id, user_id, first_rel)
    second_ref = workspace_figure_ref(tenant_id, user_id, second_rel)
    assert is_cacheable_image_ref(first_ref) is True
    assert is_cacheable_image_ref(second_ref) is True
    assert first_ref != second_ref


@pytest.mark.anyio
async def test_a_real_read_page_ref_is_recognised_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回修第 5 轮 I-1 —— 判据收窄成认**路径形状**之后,最容易犯的错是严过头:
    把功能本身关掉了还不知道(渲染页从此一条都不进缓存,每轮退化成一次 NAS 读,
    而且没有任何测试会红)。

    所以这条测试**不手写样本路径** —— out_rel 取自宿主真跑一次
    ``ReadPageTool.call`` 实际喂给片段的那个值,尾巴取自片段真跑一次产出的那个
    rel,两头都是真源拼的,中间只是把它们接起来。
    """
    _install_office_stubs(tmp_path, monkeypatch)
    (tmp_path / "d.pptx").write_text("source")

    out_rel = await _real_out_rel()
    env = _run_render(tmp_path, "d.pptx", units=[3], out_rel=out_rel)
    assert env["ok"] is True
    rel = env["rendered"][0]["rel"]

    tenant_id, user_id = uuid4(), uuid4()
    assert is_cacheable_image_ref(workspace_figure_ref(tenant_id, user_id, rel)) is True

    # 绑了 agent 的 run:ref 里多一层 agents/<key>/,判据必须在剥掉作用域前缀
    # **之后**才比形状(回修 C1 定的口径),否则渲染页全判成不可缓存。
    scoped = scoped_path(store_scope("/workspace", agent_key="pf-probe-33086dc0"), rel)
    assert is_cacheable_image_ref(workspace_figure_ref(tenant_id, user_id, scoped)) is True


@pytest.mark.anyio
async def test_a_file_the_model_wrote_under_tool_results_is_not_cacheable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回修第 5 轮 I-1 —— 第三个写入者是**模型自己**。

    ``.tool_results`` 不在任何写保护集合里,``write_file`` 对
    ``.tool_results/evil.jpg`` 是放行的;模型再把那条 ref 交给 ``ask_image``,
    租户/用户/agent_key 三项校验全对得上(文件就是它自己写的)。目录前缀通行证
    等于把"可缓存"发给了一个随时会被覆盖的文件 —— 写 A、读到 A、覆盖成 B、
    **仍然读到 A**,与 New-I1 逐字同病。

    连"藏在真实渲染目录里、只有文件名不对"这种也要挡住:形状是整条路径的形状,
    不是前缀。
    """
    _install_office_stubs(tmp_path, monkeypatch)
    (tmp_path / "d.pptx").write_text("source")
    out_rel = await _real_out_rel()
    real_rel = _run_render(tmp_path, "d.pptx", units=[3], out_rel=out_rel)["rendered"][0]["rel"]

    tenant_id, user_id = uuid4(), uuid4()
    model_written = [
        ".tool_results/evil.jpg",
        f"{out_rel}/evil.jpg",
        # 摆在真实 unit 目录里、只是文件名不合 pdftoppm 的产出约定
        real_rel.rsplit("/", 1)[0] + "/evil.jpg",
    ]
    for rel in model_written:
        ref = workspace_figure_ref(tenant_id, user_id, rel)
        assert is_cacheable_image_ref(ref) is False, f"{rel!r} 不该被当成渲染页缓存"


def test_dpi_is_part_of_the_render_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回修第 5 轮 M-1 —— 变了会让产出字节变、而 rel 不变的量,一个都不能留在
    路径外面。

    ``dpi`` 是 ``build_render_wrapper`` 的公开参数(带默认值)。今天生产路径写死
    ``RENDER_DPI`` 所以不可达,但"公开入口的前置条件没人执行就等于没有"正是审计 B
    刚关掉的那个形状 —— 靠 docstring 钉不住,得让不变式结构上成立。
    """
    _install_office_stubs(tmp_path, monkeypatch, pdftoppm_body=_PDFTOPPM_STAMPS_ITS_DPI)
    (tmp_path / "d.pdf").write_text("same bytes both times")
    out_rel = ".tool_results/r1/figures/dpi"

    low = _run_render(tmp_path, "d.pdf", units=[3], out_rel=out_rel, dpi=100)
    high = _run_render(tmp_path, "d.pdf", units=[3], out_rel=out_rel, dpi=300)

    assert low["ok"] is True
    assert high["ok"] is True
    low_rel, high_rel = low["rendered"][0]["rel"], high["rendered"][0]["rel"]
    assert (tmp_path / low_rel).read_text() == "JPEG-AT-DPI-100"
    assert (tmp_path / high_rel).read_text() == "JPEG-AT-DPI-300"
    assert low_rel != high_rel, f"同一份文件、不同 dpi,产出字节不同而 rel 一模一样({low_rel!r})"
    # 低 dpi 那一版必须原地还在 —— 高 dpi 那次没有把它覆盖掉。
    assert (tmp_path / low_rel).read_text() == "JPEG-AT-DPI-100"


#: 第一次调用正常产出,第二次(及以后)调用退出码 1、不产任何文件——模拟一次
#: 真实的渲染失败(回修第 2 轮 New-2 回归钉)。
_PDFTOPPM_FAILS_ON_ITS_SECOND_CALL = "if count >= 2:\n    sys.exit(1)\n" + _PDFTOPPM_WRITES_A_JPEG


def test_render_wrapper_deduplicates_units_so_rendered_never_holds_a_dead_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回修第 4 轮 审计 B —— ``build_render_wrapper`` 是模块级公开入口,它
    docstring 里写死的前置条件("units 不重复")必须由它自己执行。

    重复 unit 下片段会对同一个 ``_u<unit>`` 目录跑两遍:第 1 圈渲成功、rel 记
    进 ``rendered``;第 2 圈先 ``rmtree`` 把第 1 圈的产物删了,这次再失败,回出
    来的就是 ``ok: true`` + 同一个 unit 同时躺在 ``rendered`` 和 ``failed`` +
    ``rendered`` 里那条 rel 指向已被删除的文件 —— 一个自相矛盾且自信的信封,
    代价是"一条死 ref 被当成成功交给模型"。生产路径上 ``_require_units`` 会
    先去重,但那是调用方的去重,这个公开入口自己没有执行过这条前置条件。
    """
    _install_office_stubs(tmp_path, monkeypatch, pdftoppm_body=_PDFTOPPM_FAILS_ON_ITS_SECOND_CALL)
    (tmp_path / "d.pdf").write_text("fake pdf bytes")

    env = _run_render(tmp_path, "d.pdf", units=[3, 3], out_rel=".tool_results/r1/figures/dup")

    assert env["ok"] is True
    for item in env["rendered"]:
        assert (tmp_path / item["rel"]).exists(), f"rendered 里是一条死 ref:{item['rel']!r}"
    assert [item["unit"] for item in env["rendered"]] == [3]
    assert env["failed"] == []


def test_wrapper_does_not_reuse_a_stale_file_when_a_later_render_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回修第 2 轮 New-2 —— ``out_dir`` 跨调用存活:第一次渲染成功留下
    ``page-03.jpg``;第二次对同一个 out_rel 再渲同一个 unit,这次 pdftoppm
    真实失败(非零退出、不产文件)。私有 unit 目录如果不在渲染前清空,第二次
    的 glob 会把第一次留下的旧文件当成这次的产出报成功——如果这期间 rel 指
    向的文档已经换了内容(同一个 run 里 d.pdf 被覆盖),报的就是旧文档的页,
    不是新文档的页。第二次必须落进 ``failed``,不是 ``rendered``。
    """
    _install_office_stubs(tmp_path, monkeypatch, pdftoppm_body=_PDFTOPPM_FAILS_ON_ITS_SECOND_CALL)
    (tmp_path / "d.pdf").write_text("first version")
    out_rel = ".tool_results/r1/figures/abc"

    first = _run_render(tmp_path, "d.pdf", units=[3], out_rel=out_rel)
    assert first["ok"] is True
    assert first["rendered"][0]["rel"].endswith("page-03.jpg")

    second = _run_render(tmp_path, "d.pdf", units=[3], out_rel=out_rel)
    assert second["ok"] is False
    assert second["error"] == "render_failed"
    assert second["failed"] == [{"unit": 3, "why": "pdftoppm_failed"}]


#: 第一次调用按 2 位补零产出(退出码 0);第二次调用(模拟同一个 rel 指向的文档
#: 换成了总页数不同的另一份,pdftoppm 的补零宽度也跟着变了)按 3 位补零产出、
#: **同样退出码 0**——真实成功,不触发 New-2 的 ``returncode != 0`` 分支。
_PDFTOPPM_SHIFTS_ITS_PADDING_WIDTH = (
    "width = 2 if count < 2 else 3\n"
    'with open(f"{prefix}-{unit:0{width}d}.jpg", "w") as fh:\n'
    '    fh.write(f"JPEG-PAGE-{unit}::round{count}::{pdf_content}")\n'
)


def test_wrapper_does_not_pick_a_stale_file_when_a_later_render_succeeds_differently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回修第 2 轮 New-2(rmtree 那一半,独立于 returncode 检查自证)—— 第二次
    调用 pdftoppm **真的成功了**(退出码 0),只是这次补零宽度与第一次不同
    (``page-003.jpg`` 而不是 ``page-03.jpg``)。不清空私有目录的话,第一次
    留下的 ``page-03.jpg`` 仍然待在目录里,字典序排序会让 ``sorted(...)[-1]``
    挑中它而不是这次真正新产出的 ``page-003.jpg``。"""
    _install_office_stubs(tmp_path, monkeypatch, pdftoppm_body=_PDFTOPPM_SHIFTS_ITS_PADDING_WIDTH)
    (tmp_path / "d.pdf").write_text("first version")
    out_rel = ".tool_results/r1/figures/abc"

    first = _run_render(tmp_path, "d.pdf", units=[3], out_rel=out_rel)
    assert first["ok"] is True
    assert first["rendered"][0]["rel"].endswith("page-03.jpg")

    second = _run_render(tmp_path, "d.pdf", units=[3], out_rel=out_rel)
    assert second["ok"] is True
    assert second["rendered"][0]["rel"].endswith("page-003.jpg"), (
        f"expected this round's fresh page-003.jpg, got stale {second['rendered'][0]['rel']!r}"
    )
    content = (tmp_path / second["rendered"][0]["rel"]).read_text()
    assert "round2" in content


#: 第一次调用真实成功产出 pdf;第二次(及以后)调用退出码 1、不产任何文件——
#: 模拟一次真实的转换失败(回修第 3 轮 Important-1,T1 同形但落在 conv 层)。
_SOFFICE_FAILS_ON_ITS_SECOND_CALL = (
    "if count >= 2:\n"
    "    sys.exit(1)\n"
    'with open(out_pdf, "w") as fh:\n'
    '    fh.write("PDF-OF:VERSION-" + str(count))\n'
)


def test_wrapper_does_not_reuse_a_stale_pdf_when_a_later_conversion_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回修第 3 轮 Important-1(T1 同形,落在 conv 层)—— ``out_dir`` 跨调用
    存活:第一次转换成功留下 ``conv/d.pdf``;第二次对同一个 out_rel 再转同一份
    文档,这次 soffice 真实失败(非零退出、不产 pdf)。conv 目录如果不在转换
    前清空,第二次的 glob 会把第一次留下的旧 pdf 当成这次的转换结果报成功——
    如果这期间 rel 指向的文档已经换了内容,报的就是旧文档的页,不是新文档的
    页(与 New-2 逐字相同的病,只是换了一层)。第二次必须落进
    ``convert_failed``,不是 ``ok``。"""
    _install_office_stubs(tmp_path, monkeypatch, soffice_body=_SOFFICE_FAILS_ON_ITS_SECOND_CALL)
    (tmp_path / "d.pptx").write_text("source")
    out_rel = ".tool_results/r1/figures/conv1"

    first = _run_render(tmp_path, "d.pptx", units=[1], out_rel=out_rel)
    assert first["ok"] is True

    second = _run_render(tmp_path, "d.pptx", units=[1], out_rel=out_rel)
    assert second["ok"] is False
    assert second["error"] == "convert_failed"
    assert second["detail"] == "exit=1"


#: 第一次调用真实成功产出 pdf;第二次调用退出码 **0**(报告成功)却不产出任何
#: 文件——模拟一次"报了成功但没真的重写"的转换(真实 LibreOffice 也观测到过这
#: 种形态)。returncode 检查管不到这种场景,只有清空 conv 目录能让 glob 在这一
#: 轮找不到任何 pdf、从而正确报失败。
_SOFFICE_REPORTS_SUCCESS_WITHOUT_WRITING_ON_ITS_SECOND_CALL = (
    "if count >= 2:\n"
    "    sys.exit(0)\n"
    'with open(out_pdf, "w") as fh:\n'
    '    fh.write("PDF-OF:VERSION-" + str(count))\n'
)


def test_wrapper_does_not_pick_a_stale_pdf_when_a_later_conversion_reports_success_without_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回修第 3 轮 Important-1(rmtree 那一半,独立于 returncode 检查自证,
    与 T2 同一房规)—— 第二次 soffice 调用报告成功(退出码 0)却没有真的重写
    conv 目录里的 pdf。returncode 检查这半段管不到"报了成功却没产出"的场景;
    只有清空 conv 目录这一半修法能防止 glob 把第一次的旧 pdf 当成这次的转换
    结果。"""
    _install_office_stubs(
        tmp_path,
        monkeypatch,
        soffice_body=_SOFFICE_REPORTS_SUCCESS_WITHOUT_WRITING_ON_ITS_SECOND_CALL,
    )
    (tmp_path / "d.pptx").write_text("source")
    out_rel = ".tool_results/r1/figures/conv2"

    first = _run_render(tmp_path, "d.pptx", units=[1], out_rel=out_rel)
    assert first["ok"] is True

    second = _run_render(tmp_path, "d.pptx", units=[1], out_rel=out_rel)
    assert second["ok"] is False
    assert second["error"] == "convert_failed"
    assert "detail" not in second  # 走的是"没有 pdf"分支,不是 returncode 分支


#: soffice 桩:**写了一份(截断的)pdf 之后再非零退出**。这是 returncode 检查
#: 唯一不可替代的场景 —— "没产出文件"那一半兜底在这里完全失效(文件确实产出
#: 了),只有接住返回码才拦得住(回修第 4 轮 New-M5)。
_SOFFICE_WRITES_A_PARTIAL_PDF_THEN_FAILS = (
    'with open(out_pdf, "w") as fh:\n    fh.write("TRUNCATED-PARTIAL-PDF")\nsys.exit(1)\n'
)
#: pdftoppm 桩:同形 —— 写了一份截断的 jpeg 之后再非零退出。
_PDFTOPPM_WRITES_A_PARTIAL_JPEG_THEN_FAILS = (
    'with open(f"{prefix}-{unit:02d}.jpg", "w") as fh:\n'
    '    fh.write("TRUNCATED-PARTIAL-JPEG")\n'
    "sys.exit(1)\n"
)


def test_wrapper_fails_when_soffice_exits_nonzero_after_writing_a_partial_pdf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回修第 4 轮 New-M5 —— 现有两条 conv 测试里"exit 1"那条其实被 ``_fresh_dir``
    那半段兜住了(去掉 returncode 检查后 ``ok is False`` 仍然成立,只是 detail
    变了),returncode 那一半的区分全压在 ``detail == "exit=1"`` 这一行上。
    returncode 检查**唯一**不可替代的场景是"非零退出却产出了(部分)pdf"——
    产物在,清空也清了,靠"有没有产出文件"反推只会反推成功。"""
    _install_office_stubs(
        tmp_path, monkeypatch, soffice_body=_SOFFICE_WRITES_A_PARTIAL_PDF_THEN_FAILS
    )
    (tmp_path / "d.pptx").write_text("source")

    env = _run_render(tmp_path, "d.pptx", units=[1], out_rel=".tool_results/r1/figures/partial")

    assert env == {"ok": False, "error": "convert_failed", "detail": "exit=1"}


def test_wrapper_fails_when_pdftoppm_exits_nonzero_after_writing_a_partial_jpeg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回修第 4 轮 New-M5(pdftoppm 侧同形)—— 单页渲染非零退出却留下了一份
    截断的 jpeg:``_fresh_dir`` 已经清过目录、glob 也确实能捞到这份新文件,
    只有接住返回码才认得出这是一次失败。"""
    _install_office_stubs(
        tmp_path, monkeypatch, pdftoppm_body=_PDFTOPPM_WRITES_A_PARTIAL_JPEG_THEN_FAILS
    )
    (tmp_path / "d.pdf").write_text("fake pdf bytes")

    env = _run_render(tmp_path, "d.pdf", units=[3], out_rel=".tool_results/r1/figures/partial")

    assert env == {
        "ok": False,
        "error": "render_failed",
        "failed": [{"unit": 3, "why": "pdftoppm_failed"}],
    }


def test_wrapper_reports_a_clean_failure_when_the_conv_dir_is_a_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回修第 4 轮 New-M4 —— ``_fresh_dir(conv)`` 的返回值检查此前零回归覆盖
    (把 ``if not _fresh_dir(conv):`` 变异成裸 ``_fresh_dir(conv)`` 后整个
    orchestrator 测试套全绿)。与 unit 目录那条同形:conv 目录如果是指向别处
    的目录符号链接,``rmtree(ignore_errors=True)`` 拒绝 follow、异常被吞,
    ``makedirs(exist_ok=True)`` 也不报错,于是"已经清空"这个前提悄悄落空,
    glob 会把那边的旧 pdf 当成这一轮的转换结果。

    conv 目录在**内容哈希**那一层下面(回修第 4 轮 New-I1),同样不手算哈希:
    先真跑一次拿到产出 rel,照着它定位 conv 目录再换成符号链接。"""
    _install_office_stubs(tmp_path, monkeypatch)
    (tmp_path / "d.pptx").write_text("source")
    out_rel = ".tool_results/r1/figures/convlink"

    first = _run_render(tmp_path, "d.pptx", units=[1], out_rel=out_rel)
    assert first["ok"] is True
    conv_dir = (tmp_path / first["rendered"][0]["rel"]).parent.parent / "_pdf"
    assert conv_dir.is_dir()

    evil_dir = tmp_path / "evil_conv"
    evil_dir.mkdir()
    (evil_dir / "stale.pdf").write_text("STALE-PDF-FROM-ELSEWHERE")
    shutil.rmtree(conv_dir)
    conv_dir.symlink_to(evil_dir, target_is_directory=True)

    env = _run_render(tmp_path, "d.pptx", units=[1], out_rel=out_rel)

    assert env == {"ok": False, "error": "convert_failed", "detail": "conv_dir_not_clean"}
    # 别处那个目录必须原封不动——不能被当成这次的产出改动或删除。
    assert (evil_dir / "stale.pdf").read_text() == "STALE-PDF-FROM-ELSEWHERE"


def test_wrapper_reports_a_clean_failure_when_the_unit_dir_is_a_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回修第 3 轮 Minor-3 —— 私有产出目录如果是指向别处的目录符号链接,
    ``shutil.rmtree(..., ignore_errors=True)`` 拒绝 follow 符号链接、异常被
    ``ignore_errors`` 吞掉,``os.makedirs(exist_ok=True)`` 对一个符号链接指向
    的已存在目录也不会报错——"已经清空"这个前提会悄悄落空,旧目录(这里模拟
    成上一轮/攻击者留下的另一个目录)里的旧文件原样还在,足以让 New-2 的坑
    以第三种方式重开。必须播报成一次真实失败,不能被 glob 到、当成这次的
    产出。

    unit 目录现在在**内容哈希**那一层下面(回修第 4 轮 New-I1),测试不去手算
    那一段哈希(那就成了第二份真源)—— 先真跑一次拿到产出 rel,再照着它把
    ``_u3`` 换成符号链接,然后跑第二次。"""
    _install_office_stubs(tmp_path, monkeypatch)
    (tmp_path / "d.pdf").write_text("fake pdf bytes")
    out_rel = ".tool_results/r1/figures/symlinked"

    first = _run_render(tmp_path, "d.pdf", units=[3], out_rel=out_rel)
    assert first["ok"] is True
    unit_dir = (tmp_path / first["rendered"][0]["rel"]).parent
    assert unit_dir.name == "_u3"

    evil_dir = tmp_path / "evil"
    evil_dir.mkdir()
    (evil_dir / "page-99.jpg").write_text("STALE-FROM-ELSEWHERE")
    shutil.rmtree(unit_dir)
    unit_dir.symlink_to(evil_dir, target_is_directory=True)

    env = _run_render(tmp_path, "d.pdf", units=[3], out_rel=out_rel)

    assert env == {
        "ok": False,
        "error": "render_failed",
        "failed": [{"unit": 3, "why": "unit_dir_not_clean"}],
    }
    # 旧目录必须原封不动——不能被当成这次的产出改动或删除。
    assert (evil_dir / "page-99.jpg").read_text() == "STALE-FROM-ELSEWHERE"


# ---------------------------------------------------------------------------
# Task 4b —— docx 的段落序号 -> PDF 页号。
#
# 这一段测的是本功能的**核心风险**:解析错一页 = 渲出一张无关页并声称它就是
# 模型问的那处图 —— 正是 Task 4 的 C3 当初把 docx 拒掉的理由。所以每一条要么
# 钉住"渲对了哪一页",要么钉住"没渲、而且说了具名原因"。
# ---------------------------------------------------------------------------


def _rendered_jpegs(tmp_path: Path) -> list[Path]:
    return sorted(tmp_path.glob("**/*.jpg"))


def _pdftoppm_was_called(bin_dir: Path) -> bool:
    return (bin_dir / "pdftoppm_calls").exists()


def test_docx_unit_resolves_to_the_page_pdfimages_pins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """基本档:anchor 在第 2 页、``pdfimages`` 也把图报在第 2 页 -> 渲第 2 页。

    同时钉住 ``rendered`` 里 ``unit``(段落序号)与 ``page``(真渲了哪一页)
    是**两个**字段:宿主拿 unit 冒充页号就会告诉模型一个不存在的页码。
    """
    _install_office_stubs(
        tmp_path,
        monkeypatch,
        pdftotext_body=_pdftotext_pages("Cover page body text.", _ANCHOR_ONE, "Trailing page."),
        pdfimages_body=_pdfimages_rows([(2, *_BIG_ROW)]),
    )
    _write_docx(tmp_path / "d.docx", _para(_ANCHOR_ONE), _figure())

    env = _run_render(tmp_path, "d.docx", units=[2], out_rel=".tool_results/r1/figures/s")

    assert env["ok"] is True
    item = env["rendered"][0]
    assert (item["unit"], item["page"]) == (2, 2)
    assert item["rel"].endswith("page-02.jpg")
    assert "note" not in item


def test_docx_figure_pushed_over_the_page_break_is_rendered_on_the_next_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**页边界** —— 只用 anchor 就会错的那一条,必须有。

    anchor 那段文字排在第 2 页页底,图被挤到了第 3 页。第 1 步给出的邻域页是
    2,第 2 步的 ``pdfimages`` 把它纠正到 3。拿掉第 2 步(或让它不起作用),
    模型会拿到第 2 页 —— 一张不含目标图、却被声称是那处图的页。
    """
    _install_office_stubs(
        tmp_path,
        monkeypatch,
        pdftotext_body=_pdftotext_pages("Cover page body text.", _ANCHOR_ONE, "Continued."),
        pdfimages_body=_pdfimages_rows([(3, *_BIG_ROW)]),
    )
    _write_docx(tmp_path / "d.docx", _para(_ANCHOR_ONE), _figure())

    env = _run_render(tmp_path, "d.docx", units=[2], out_rel=".tool_results/r1/figures/s")

    assert env["ok"] is True
    item = env["rendered"][0]
    assert item["page"] == 3, f"页边界没纠正过来:{item}"
    assert item["rel"].endswith("page-03.jpg")


@pytest.mark.parametrize("decorative", [_LOGO_ROW, _STRIP_ROW], ids=["square_logo", "thin_strip"])
def test_docx_decorative_rows_do_not_pin_the_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, decorative: tuple[int, int, int, int]
) -> None:
    """装饰行不许把第 2 步压成"永远返回邻域页"(与简报的偏离,见报告)。

    页眉/页脚的图住在 ``word/header1.xml``,**清单根本看不见它**(``_docx_inventory``
    只读 ``word/document.xml``),而它在渲出来的 PDF 里**每一页都有一行**
    (实测:同一个 XObject 被 N 页引用就出 N 行)。不按尺寸把这些行滤掉的话,
    "邻域里的第一行"恒等于邻域页自己,上面那条页边界测试对所有带页眉图的文档
    都会静默失效 —— 而带页眉 logo 的公文正是最常见的一种 docx。

    两种形状都跑(回修第 1 轮 I-2/N1):方 logo 的短边等于长边,**分不出**
    "按短边判"和"按长边判";窄色带(602x10pt)才分得出来 —— 清单侧为同一件事
    立过 ``test_thin_strip_is_skipped_by_the_edge_clause``,渲染侧当时没立。
    """
    bin_dir = _install_office_stubs(
        tmp_path,
        monkeypatch,
        pdftotext_body=_pdftotext_pages("Cover page body text.", _ANCHOR_ONE, "Continued."),
        pdfimages_body=_pdfimages_rows(
            [(1, *decorative), (2, *decorative), (3, *decorative), (3, *_BIG_ROW)]
        ),
    )
    _write_docx(tmp_path / "d.docx", _para(_ANCHOR_ONE), _figure())

    env = _run_render(tmp_path, "d.docx", units=[2], out_rel=".tool_results/r1/figures/s")

    assert env["ok"] is True
    item = env["rendered"][0]
    assert item["page"] == 3, "装饰行把页号钉在了邻域页上"
    assert "note" not in item, "装饰行没被滤掉,于是两页都算有图"
    assert _pdftoppm_was_called(bin_dir)


# ---------------------------------------------------------------------------
# 回修第 1 轮 C-1 —— 尺寸滤子**只**挡得住短边低于阈值的装饰图。抬头 banner
# (450x60pt,短边 60pt > 40pt)滤不掉,于是"邻域里有没有位图"这个判据下,
# 跨页的图会被静默钉回锚点页且**不带任何 note**:模型连"这可能不准"都不知道。
# 判据因此改成"邻域里的位图**能不能唯一指认**这一处图"。
# ---------------------------------------------------------------------------


def test_docx_banner_on_every_page_still_renders_the_anchor_page_but_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C-1 场景 A:抬头 banner 每页一份 + 目标图在下一页。

    banner 短边 60pt 高于装饰图阈值,滤不掉,所以锚点页与下一页**都**有存活行。
    本地分不出哪一行是目标图,于是仍渲锚点页(先验上更可能),但**必须**带
    歧义 note —— 这一档以前静默返回锚点页,是 C-1 的正题。
    """
    _install_office_stubs(
        tmp_path,
        monkeypatch,
        pdftotext_body=_pdftotext_pages("Cover page body text.", _ANCHOR_ONE, "Continued."),
        pdfimages_body=_pdfimages_rows(
            [(1, *_BANNER_ROW), (2, *_BANNER_ROW), (3, *_BANNER_ROW), (3, *_BIG_ROW)]
        ),
    )
    _write_docx(tmp_path / "d.docx", _para(_ANCHOR_ONE), _figure())

    env = _run_render(tmp_path, "d.docx", units=[2], out_rel=".tool_results/r1/figures/s")

    assert env["ok"] is True
    item = env["rendered"][0]
    assert item["page"] == 2
    assert item["note"] == "figure_page_ambiguous_fallback"


def test_docx_another_real_figure_on_the_anchor_page_is_flagged_as_ambiguous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C-1 场景 B:锚点页本身另有一张真图,目标图被挤到了下一页。

    与场景 A 同根:邻域两页都有存活行,指认不唯一。上一轮报告把这一档写成了
    "已知残留、只能记账",实际上它与 A 共用同一条判据,一起兜住了 —— 渲锚点页
    但把保留意见说出来。
    """
    _install_office_stubs(
        tmp_path,
        monkeypatch,
        pdftotext_body=_pdftotext_pages("Cover page body text.", _ANCHOR_ONE, "Continued."),
        pdfimages_body=_pdfimages_rows([(2, *_BIG_ROW), (3, *_BIG_ROW)]),
    )
    _write_docx(tmp_path / "d.docx", _para(_ANCHOR_ONE), _figure())

    env = _run_render(tmp_path, "d.docx", units=[2], out_rel=".tool_results/r1/figures/s")

    assert env["ok"] is True
    item = env["rendered"][0]
    assert item["page"] == 2
    assert item["note"] == "figure_page_ambiguous_fallback"


def test_render_side_decorative_threshold_is_the_public_constant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """渲染侧的装饰图阈值必须**真的**是 ``MIN_FIGURE_EDGE_PT``(回修第 1 轮 I-3/N4)。

    实测过:把公开常量改成 1.0,全套**全绿存活** —— 它零生产消费者,而
    ``document_figures.py`` 的注释写着"低于此的判为装饰图",照那句去调的人
    什么都调不动。现在它由宿主喂进片段,这条测试把它钉活:把阈值压到 1.0,
    原本被当装饰图丢掉的 logo 行就该活下来,于是锚点页也算"有图",两页都有
    → 歧义 note。片段里要是再手写一份 40.0,这条立刻红。
    """
    monkeypatch.setattr(read_page, "MIN_FIGURE_EDGE_PT", 1.0)
    _install_office_stubs(
        tmp_path,
        monkeypatch,
        pdftotext_body=_pdftotext_pages("Cover page body text.", _ANCHOR_ONE, "Continued."),
        pdfimages_body=_pdfimages_rows([(2, *_LOGO_ROW), (3, *_BIG_ROW)]),
    )
    _write_docx(tmp_path / "d.docx", _para(_ANCHOR_ONE), _figure())

    env = _run_render(tmp_path, "d.docx", units=[2], out_rel=".tool_results/r1/figures/s")

    assert env["ok"] is True
    item = env["rendered"][0]
    assert item["page"] == 2
    assert item["note"] == "figure_page_ambiguous_fallback", (
        "阈值压到 1.0 之后 logo 行仍被丢掉 —— 片段没在用那个公开常量"
    )


def test_both_snippets_get_the_thresholds_from_the_same_constants() -> None:
    """两个 wrapper 喂进片段的阈值必须来自**同一对**公开常量(回修第 1 轮 I-3)。

    上一轮报告把这里的风险描述错了(说两侧"会分叉成页号选错"),实际上两侧读的
    是同一个片段里的同一行,结构上分叉不了。真正的缺陷是那两个公开常量当时
    **零生产消费者**。这条钉的是修好之后的形状:两个 wrapper 的 ``_P`` 里都带着
    它们,而且带的就是模块常量的值。
    """
    render_params = _snippet_params(
        build_render_wrapper("d.docx", units=[1], ws="/workspace", out_rel=".tool_results/r1/f/s")
    )
    inventory_params = _snippet_params(
        build_figure_inventory_wrapper("d.docx", ws="/workspace", max_bytes=1024)
    )
    for params in (render_params, inventory_params):
        assert params["min_figure_edge_pt"] == MIN_FIGURE_EDGE_PT
        assert params["min_figure_area_ratio"] == MIN_FIGURE_AREA_RATIO
    # read_page 侧不许自己再声明一份同名常量 —— 身份而不是相等。
    assert read_page.MIN_FIGURE_EDGE_PT is MIN_FIGURE_EDGE_PT
    assert read_page.MIN_FIGURE_AREA_RATIO is MIN_FIGURE_AREA_RATIO


@pytest.mark.parametrize(
    ("slow_binary", "expected_error"),
    [("pdftotext", "pdftotext_failed"), ("pdfimages", "pdfimages_failed")],
    ids=["pdftotext", "pdfimages"],
)
def test_docx_page_resolvers_use_the_short_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, slow_binary: str, expected_error: str
) -> None:
    """两件 poppler 解析工具必须用 ``render_timeout_s``,不是 ``convert_timeout_s``
    (回修第 1 轮 I-1 的**行为**证人)。

    ``test_internal_render_budget_fits_under_the_exec_timeout_cap`` 只对两个常量
    做算术:把片段里的 ``render_timeout_s`` 换回 ``convert_timeout_s``,那条算术
    照样成立、**全套全绿** —— 实测变异存活过一次,是一条重言式。真正咬得住的是
    行为:把两个 timeout 拉开,再让这个桩睡到**夹在两者中间**的时长 —— 用短的
    会超时并具名失败,用长的会若无其事地跑完。

    回修第 2 轮 I-A —— 余量必须给足。第一版用的是 ``render=1s`` + ``sleep(3)``,
    ``[pdfimages]`` 那一档因此要求**先跑的 pdftotext 桩在 1 秒内跑完**;实测桩
    启动 median 18ms 但 **max 2.039s**,尾巴本身就超预算。串行与 ``-n 4`` 都绿,
    单文件 ``-n 8`` 下**六次全红**(报成 ``pdftotext_failed``)。CI 跑的是
    ``-n auto``,核数一变就会开始间歇性假红 —— 而本仓每一次假红都要有人去分辨
    它是证人还是病人,这条又恰恰埋在本轮最重要的行为钉上。
    回修第 3 轮 Important-1 —— **界在哪一侧,我上一轮指错了**。当时写"真正的界是
    ``sleep`` 与 ``render`` 之间那段";不成立:``sleep`` 是墙钟,**不可能提前跑完**,
    那一侧根本不构成风险。会咬人的是**另一侧**:``[pdfimages]`` 这一档里,
    **先跑的那个健康 pdftotext 桩必须在 ``render_timeout_s`` 内跑完**,否则
    报成 ``pdftotext_failed``、断言直接红 —— 第一轮六次全红死的正是这条。
    而且"中位数的 280 倍"是拿**中位数**说的,中位数恰恰是第一轮判断失误时用的
    那个统计量。实测 8 路并发 spawn 同形桩 x400:median **0.020s** /
    p95 **0.029s** / **p99 3.149s / max 3.155s** —— ``render=5s`` 对 p99 只有
    约 **1.6 倍**余量。余量要按**尾部**算,不是按中位数。
    现在是 ``render=15s`` / ``sleep(45)`` / ``convert=90s``:对 p99 约 4.8 倍,
    慢桩仍夹在 ``15 < 45 < 90`` 中间,杀变异能力不变。代价:这条 ~11s 变 ~30s。
    """
    slow = "import time\ntime.sleep(45)\n"
    bodies: dict[str, str] = {
        "pdftotext": _pdftotext_pages("Cover page body text.", _ANCHOR_ONE),
        "pdfimages": _pdfimages_rows([(2, *_BIG_ROW)]),
    }
    bodies[slow_binary] = slow + bodies[slow_binary]
    _install_office_stubs(
        tmp_path,
        monkeypatch,
        pdftotext_body=bodies["pdftotext"],
        pdfimages_body=bodies["pdfimages"],
    )
    _write_docx(tmp_path / "d.docx", _para(_ANCHOR_ONE), _figure())

    env = _run_render(
        tmp_path,
        "d.docx",
        units=[2],
        out_rel=".tool_results/r1/figures/s",
        convert_timeout_s=90,
        render_timeout_s=15,
    )

    assert env == {"ok": False, "error": expected_error}


# ---------------------------------------------------------------------------
# 回修第 2 轮 I-C —— 邻域里的行先**归因**再数。
#
# 不归因的话,一份「一页一张图」的普通报告(体检报告、趋势图册、图文教程都是
# 这个形状)3 张里会有 2 张挂上歧义 note,而 3 张的页号**全是对的**。代价两层:
# note 劝模型再取一页,单 run 像素预算(≈10 页 @100dpi)被砍掉近一半;更坏的是
# **一条常响的警告不是警告** —— 模型学会忽略它之后就绕一圈回到「该有信号处无信号」。
# ---------------------------------------------------------------------------


def test_docx_one_figure_per_page_report_is_quiet_and_correct(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """一页一图三连:三处都必须**页号对**且**不响 note**。

    每一页上那一行,都能被清单里"锚点页正好是这一页"的那条条目解释掉,所以对
    相邻的那一处图来说它不是"来历不明的第二张图"。
    """
    _install_office_stubs(
        tmp_path,
        monkeypatch,
        pdftotext_body=_pdftotext_pages("Alpha caption one.", "Beta caption two.", "Gamma three."),
        pdfimages_body=_pdfimages_rows([(1, *_BIG_ROW), (2, *_BIG_ROW), (3, *_BIG_ROW)]),
    )
    _write_docx(
        tmp_path / "d.docx",
        _para("Alpha caption one."),
        _figure(),
        _para("Beta caption two."),
        _figure(),
        _para("Gamma three."),
        _figure(),
    )

    got = {}
    for unit, out in ((2, "a"), (4, "b"), (6, "c")):
        env = _run_render(tmp_path, "d.docx", units=[unit], out_rel=f".tool_results/r1/f/{out}")
        assert env["ok"] is True, unit
        item = env["rendered"][0]
        got[unit] = (item["page"], item.get("note"))

    assert got == {2: (1, None), 4: (2, None), 6: (3, None)}, (
        f"一页一图的普通报告不该响歧义 note、页号也不该错:{got}"
    )


def test_docx_next_page_row_claimed_by_a_later_entry_is_not_ambiguous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """归因**仍然**起作用的那一档:认领方的锚点页与目标不同(``a1 != a2``)。

    文档:第 1 段文字 + 第 2 段图 B(锚点页 2),第 3 段文字 + 第 4 段图 C
    (锚点页 3)。PDF 第 2 页一行(B)、第 3 页一行(C)。
    问 B:邻域 {2,3};第 3 页那一行归 C(第 3 页行数 1 >= 落在第 3 页的条目数 1,
    对称性检查过),剩下第 2 页那一行 —— 钉第 2 页,**不响 note**。
    不归因的话两页都剩着行 → 白响一次 note。

    回修第 3 轮 Critical-1 —— 这条以前叫 ``..._scenario_b_now_pins_the_next_page``,
    用的是"两条条目锚点**同在**第 1 页"的夹具;那个夹具正是**互认领**的形状,
    对称性检查之后它回到"锚点页 + 响 note"(见
    ``..._same_page_entries_without_enough_rows_stay_honest``)。
    真正活下来的改善是 ``a1 != a2`` 这一族,改用它的形状。
    """
    _install_office_stubs(
        tmp_path,
        monkeypatch,
        pdftotext_body=_pdftotext_pages("Cover.", "Figure B caption.", "Figure C caption."),
        pdfimages_body=_pdfimages_rows([(2, *_BIG_ROW), (3, *_BIG_ROW)]),
    )
    _write_docx(
        tmp_path / "d.docx",
        _para("Figure B caption."),
        _figure(),
        _para("Figure C caption."),
        _figure(),
    )

    env = _run_render(tmp_path, "d.docx", units=[2], out_rel=".tool_results/r1/figures/s")

    assert env["ok"] is True
    item = env["rendered"][0]
    assert item["page"] == 2
    assert "note" not in item, "下一页那一行已归属别的条目,不该判歧义"


def test_docx_same_page_entries_without_enough_rows_stay_honest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**互认领**必须被对称性检查挡住(回修第 3 轮 Critical-1 的正题)。

    文档:两处图的锚点段落都落在第 1 页,两处图**实际也都在第 1 页**,但其中
    一处是矢量(``pdfimages`` 看不见),所以第 1 页只有 1 行;第 2 页另有一整幅
    banner(不在清单里,没人认领)。

    不做对称性检查时:问 A,第 1 页那一行被算成"B 的" → 第 1 页清空 → 只剩第 2 页
    → 钉第 2 页、**不响 note**;问 B 同理。**两条都错页、都静默**,而它们本来
    都在第 1 页。行数(1)不够落在第 1 页的条目数(2)分,"那一行是谁的"根本
    推不出来 —— 这时谁也不许认领,两条都回到"锚点页 + 响 note",页对、话也说了。
    """
    _install_office_stubs(
        tmp_path,
        monkeypatch,
        pdftotext_body=_pdftotext_pages("Figure A caption. Figure B caption.", "Banner page."),
        pdfimages_body=_pdfimages_rows([(1, *_BIG_ROW), (2, *_BANNER_ROW)]),
    )
    _write_docx(
        tmp_path / "d.docx",
        _para("Figure A caption."),
        _figure(),
        _para("Figure B caption."),
        _figure(),
    )

    got = {}
    for unit, out in ((2, "a"), (4, "b")):
        env = _run_render(tmp_path, "d.docx", units=[unit], out_rel=f".tool_results/r1/f/{out}")
        assert env["ok"] is True, unit
        item = env["rendered"][0]
        got[unit] = (item["page"], item.get("note"))

    assert got == {
        2: (1, "figure_page_ambiguous_fallback"),
        4: (1, "figure_page_ambiguous_fallback"),
    }, f"互认领把两条都静默推到了下一页:{got}"


def test_docx_unexplained_row_next_to_an_explained_one_still_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """归因只吃掉**能解释的**那些行:同一页上混着一行被认领、一行没被认领时,
    那一页仍然算"还剩着行",歧义 note 照响。

    文档:第 1 段文字 + 第 2 段图 A(锚点页 1),第 3 段文字 + 第 4 段图 B(锚点页 2)。
    PDF:第 1 页 1 行,第 2 页 **2** 行(B 的 + 一张没人认领的抬头图)。
    问 A(锚点页 1):第 1 页剩 1 行;第 2 页 2 行只有 1 行归 B,还剩 1 行 ——
    两页都剩着 → 响 note。
    """
    _install_office_stubs(
        tmp_path,
        monkeypatch,
        pdftotext_body=_pdftotext_pages("Figure A caption.", "Figure B caption."),
        pdfimages_body=_pdfimages_rows([(1, *_BIG_ROW), (2, *_BIG_ROW), (2, *_BANNER_ROW)]),
    )
    _write_docx(
        tmp_path / "d.docx",
        _para("Figure A caption."),
        _figure(),
        _para("Figure B caption."),
        _figure(),
    )

    env = _run_render(tmp_path, "d.docx", units=[2], out_rel=".tool_results/r1/figures/s")

    assert env["ok"] is True
    item = env["rendered"][0]
    assert item["page"] == 1
    assert item["note"] == "figure_page_ambiguous_fallback"


def test_docx_shared_anchor_page_next_door_does_not_make_a_third_figure_noisy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """对称性检查**只作用在锚点页**(回修第 4 轮 Important-1)。

    三图文档:图①锚点在第 1 页,图②③锚点**都在第 2 页**,而第 2 页只有 1 行
    可见行(②③里有一张是矢量)。问**图①** —— 它的邻域是 {1, 2},第 2 页上那一行
    是②或③的,**这一点是确凿的**(图①根本不落在第 2 页),该认领就认领。

    检查写宽到 ``near+1`` 上时,第 2 页因 ``rows(2)=1 < entries(2)=2`` 被一律禁止
    认领 → 两页都"剩着行" → 图①白响一次歧义 note(噪声回流);锚点页再一行都
    没有的话,答案还会从"锚点页 + 回落 note"变成"第 2 页 + **不带任何 note**"
    (新开的静默错页)。触发条件与互认领是同一个,只是多要一张相邻的图。

    ``..._one_figure_per_page_report_is_quiet_and_correct`` **结构上够不到**这个
    形状 —— 它三条锚点互不共页,``entries`` 恒为 1,对称性检查永远放行。
    """
    _install_office_stubs(
        tmp_path,
        monkeypatch,
        pdftotext_body=_pdftotext_pages("Figure A caption.", "Figure B caption. Figure C caption."),
        pdfimages_body=_pdfimages_rows([(1, *_BIG_ROW), (2, *_BIG_ROW)]),
    )
    _write_docx(
        tmp_path / "d.docx",
        _para("Figure A caption."),
        _figure(),
        _para("Figure B caption."),
        _figure(),
        _para("Figure C caption."),
        _figure(),
    )

    got = {}
    for unit, out in ((2, "a"), (4, "b"), (6, "c")):
        env = _run_render(tmp_path, "d.docx", units=[unit], out_rel=f".tool_results/r1/f/{out}")
        assert env["ok"] is True, unit
        item = env["rendered"][0]
        got[unit] = (item["page"], item.get("note"))

    assert got == {2: (1, None), 4: (2, None), 6: (2, None)}, (
        f"共锚点的那两条把不相干的图①也拖成了歧义:{got}"
    )


def test_docx_same_page_entries_do_not_both_fall_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """互认领的另一半后果,对称性检查一并解掉(回修第 3 轮 Critical-1 顺带)。

    两条条目锚点同在第 1 页、第 1 页只有一行、第 2 页**没有**行:不做对称性
    检查时,问谁都把那一行算成"对方的" → 邻域里一行都不剩 → 两条都多响一次
    ``anchor_only_fallback``,而第 1 页明明有图。行不够分时不许认领之后,
    两条都拿到第 1 页且不响 note。
    """
    _install_office_stubs(
        tmp_path,
        monkeypatch,
        pdftotext_body=_pdftotext_pages("Figure A caption. Figure B caption.", "Plain page."),
        pdfimages_body=_pdfimages_rows([(1, *_BIG_ROW)]),
    )
    _write_docx(
        tmp_path / "d.docx",
        _para("Figure A caption."),
        _figure(),
        _para("Figure B caption."),
        _figure(),
    )

    got = {}
    for unit, out in ((2, "a"), (4, "b")):
        env = _run_render(tmp_path, "d.docx", units=[unit], out_rel=f".tool_results/r1/f/{out}")
        item = env["rendered"][0]
        got[unit] = (item["page"], item.get("note"))

    assert got == {2: (1, None), 4: (1, None)}, f"互认领让两条都白响了一次回落:{got}"


def test_docx_two_rows_on_one_page_do_not_warn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同一页上剩两行**不**响 note(回修第 2 轮 Minor-1 的措辞对应的行为)。

    页号在两种解释下是同一个,没有"哪一页"的歧义可言 —— 判据是"还有几**页**
    剩着行",不是"指认唯不唯一"。
    """
    _install_office_stubs(
        tmp_path,
        monkeypatch,
        pdftotext_body=_pdftotext_pages(_ANCHOR_ONE, "Second page."),
        pdfimages_body=_pdfimages_rows([(1, *_BIG_ROW), (1, *_BIG_ROW)]),
    )
    _write_docx(tmp_path / "d.docx", _para(_ANCHOR_ONE), _figure())

    env = _run_render(tmp_path, "d.docx", units=[2], out_rel=".tool_results/r1/figures/s")

    assert env["ok"] is True
    item = env["rendered"][0]
    assert item["page"] == 1
    assert "note" not in item


def test_docx_unmeasurable_rows_are_kept_not_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """尺寸**读不出来**的行一律留着(回修第 1 轮 I-2/S2)。

    上一轮自查写了"这一条代码真的做到了",但没有任何测试咬得住:把"读不出来
    就留着"改成"读不出来就丢掉",全套仍然全绿。这里给它一个证人 —— 一行
    ppi 为 0(退化 CTM 会出这种值)的位图落在目标页上:留着它页号才钉得到
    第 3 页,丢掉它就落回锚点页。丢掉一张真图比留下一张装饰图坏,方向不能反。
    """
    _install_office_stubs(
        tmp_path,
        monkeypatch,
        pdftotext_body=_pdftotext_pages("Cover page body text.", _ANCHOR_ONE, "Continued."),
        pdfimages_body=_pdfimages_rows([(3, 200, 200, 0, 0)]),
    )
    _write_docx(tmp_path / "d.docx", _para(_ANCHOR_ONE), _figure())

    env = _run_render(tmp_path, "d.docx", units=[2], out_rel=".tool_results/r1/figures/s")

    assert env["ok"] is True
    item = env["rendered"][0]
    assert item["page"] == 3, "量不出尺寸的行被当成装饰图丢了"
    assert "note" not in item


def test_docx_vector_figure_falls_back_to_the_anchor_page_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """图表 / SmartArt / EMF 被 LibreOffice 渲成 PDF **矢量算子**,``pdfimages``
    一行都看不见(2026-09-23 实测:纯矢量页在 ``-list`` 里没有行)。这时回落到
    邻域页,但**必须**把"这一页是按文字锚点定位的"说出来 —— 不说的话模型会把
    一张可能不含目标图的页当成确凿证据。"""
    _install_office_stubs(
        tmp_path,
        monkeypatch,
        pdftotext_body=_pdftotext_pages("Cover page body text.", _ANCHOR_ONE),
        pdfimages_body=_pdfimages_rows([]),
    )
    _write_docx(tmp_path / "d.docx", _para(_ANCHOR_ONE), _figure())

    env = _run_render(tmp_path, "d.docx", units=[2], out_rel=".tool_results/r1/figures/s")

    assert env["ok"] is True
    item = env["rendered"][0]
    assert item["page"] == 2
    assert item["note"] == "anchor_only_fallback"


def test_docx_bitmap_outside_the_neighbourhood_does_not_pin_the_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """邻域之外的位图行不是这处图(与简报的偏离,见报告)。

    anchor 在第 2 页,全文下一张位图在第 **4** 页 —— 那是别处的图。"页号 >= 邻域页
    的第一行"会把它当成答案,跨出 anchor 能担保的范围。这里改成回落到邻域页 +
    说明,不拿一个远处的页硬凑。

    回修第 1 轮 I-2/N2 —— 这一行原来放在第 7 页,离边界太远:把邻域宽度从
    ``near + 1`` 放宽成 ``near + 2`` 也照样测不出来。放到 ``near + 2`` 这个
    **紧贴边界**的位置才咬得住宽度本身。
    """
    _install_office_stubs(
        tmp_path,
        monkeypatch,
        pdftotext_body=_pdftotext_pages(
            "Cover page body text.", _ANCHOR_ONE, "Continued.", "Fourth page."
        ),
        pdfimages_body=_pdfimages_rows([(4, *_BIG_ROW)]),
    )
    _write_docx(tmp_path / "d.docx", _para(_ANCHOR_ONE), _figure())

    env = _run_render(tmp_path, "d.docx", units=[2], out_rel=".tool_results/r1/figures/s")

    assert env["ok"] is True
    item = env["rendered"][0]
    assert item["page"] == 2
    assert item["note"] == "anchor_only_fallback"


def test_docx_inline_image_rows_are_read_from_the_right_edge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ppi 两列必须从**右**边数。

    内联图像(``BI … ID … EI``)那一行里 ``[inline]`` 把 ``object`` + ``ID`` 两列
    压成一个 token,整行 15 列而不是 16(2026-09-23 实测)。按左边的固定下标取
    ppi 会读到 y-ppi 和 ``size``,后者 parse 不出数字 -> 尺寸判定放弃 -> 这一行
    被当成"量不出来,留着"。

    所以能分开对错的只有"正确解析会**丢掉**它"的场景:第 2 页那张内联 logo
    显示只有 28.8 pt,滤掉之后页号才会落到第 3 页那张真图上;左数版本会把 logo
    留下,页号停在第 2 页 —— 又一次渲出不含目标图的页。
    """
    _install_office_stubs(
        tmp_path,
        monkeypatch,
        pdftotext_body=_pdftotext_pages("Cover page body text.", _ANCHOR_ONE, "Continued."),
        pdfimages_body=_pdfimages_rows([(2, *_LOGO_ROW, True), (3, *_BIG_ROW)]),
    )
    _write_docx(tmp_path / "d.docx", _para(_ANCHOR_ONE), _figure())

    env = _run_render(tmp_path, "d.docx", units=[2], out_rel=".tool_results/r1/figures/s")

    assert env["ok"] is True
    item = env["rendered"][0]
    assert item["page"] == 3, "内联行的 ppi 读错了,logo 把页号钉在了锚点页"
    assert "note" not in item


def test_docx_rounding_margin_keeps_a_row_that_might_be_a_real_figure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``-list`` 里的 ppi 是**取整**过的,反推尺寸只能给出一个区间。

    2x2 px @ 4 ppi:按取整后的 ppi 直算是 36 pt(<40,判成装饰图丢掉),按可能
    的最小 ppi(3.5)算是 41.1 pt(>=40,是真图)。误差必须一律偏向**保留** ——
    把一张真图误判成装饰图会把它从候选里删掉,页号跟着落回锚点页,而那正是
    这个功能要消灭的"渲了一张无关页还说得很确定"。
    """
    _install_office_stubs(
        tmp_path,
        monkeypatch,
        pdftotext_body=_pdftotext_pages(_ANCHOR_ONE, "Second page."),
        pdfimages_body=_pdfimages_rows([(2, 2, 2, 4, 4)]),
    )
    _write_docx(tmp_path / "d.docx", _para(_ANCHOR_ONE), _figure())

    env = _run_render(tmp_path, "d.docx", units=[2], out_rel=".tool_results/r1/figures/s")

    assert env["ok"] is True
    assert "note" not in env["rendered"][0], "落在取整误差里的行被当成装饰图丢了"


def test_docx_skipped_decorative_figures_do_not_shift_the_unit_numbers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """清单跳过装饰图时,``unit`` **不**重新编号 —— 它是段落序号,不是"第几张图"。

    这份文档的第 2 段是一张 28.8pt 的 logo(被跳过),第 4 段才是真图。清单里
    只有 unit=4 这一条;要是哪天有人把 unit 改成"清单里的第几条",这里会变成
    1,解析出来的锚点就整段错位。
    """
    bin_dir = _install_office_stubs(
        tmp_path,
        monkeypatch,
        pdftotext_body=_pdftotext_pages("Cover page body text.", _ANCHOR_TWO),
        pdfimages_body=_pdfimages_rows([(2, *_BIG_ROW)]),
    )
    _write_docx(
        tmp_path / "d.docx",
        _para(_ANCHOR_ONE),
        _figure(_LOGO_EMU),
        _para(_ANCHOR_TWO),
        _figure(),
    )

    env = _run_render(tmp_path, "d.docx", units=[4], out_rel=".tool_results/r1/figures/s")
    assert env["ok"] is True
    assert (env["rendered"][0]["unit"], env["rendered"][0]["page"]) == (4, 2)

    # 被跳过的那一段没有进清单 —— 拿它的序号来问是 honest 失败,不是渲第 2 段。
    skipped = _run_render(tmp_path, "d.docx", units=[2], out_rel=".tool_results/r1/figures/t")
    assert skipped["ok"] is False
    assert skipped["failed"] == [{"unit": 2, "why": "unit_not_in_inventory"}]
    assert _pdftoppm_was_called(bin_dir) is True  # 上面那次成功调过


@pytest.mark.parametrize(
    ("scenario", "why", "units", "body", "pages"),
    [
        (
            "anchor_empty",
            "anchor_empty",
            [1],
            (_figure(),),
            ("Cover page body text.", _ANCHOR_ONE),
        ),
        (
            "anchor_not_found",
            "anchor_not_found",
            [2],
            (_para(_ANCHOR_ONE), _figure()),
            ("Cover page body text.", "Reflowed beyond recognition."),
        ),
        (
            "anchor_ambiguous",
            "anchor_ambiguous",
            [2],
            (_para(_ANCHOR_ONE), _figure()),
            (_ANCHOR_ONE, "Middle page.", _ANCHOR_ONE),
        ),
        (
            "unit_not_in_inventory",
            "unit_not_in_inventory",
            [9],
            (_para(_ANCHOR_ONE), _figure()),
            ("Cover page body text.", _ANCHOR_ONE),
        ),
    ],
)
def test_docx_unresolvable_units_fail_by_name_and_never_render(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
    why: str,
    units: list[int],
    body: tuple[str, ...],
    pages: tuple[str, ...],
) -> None:
    """四种 honest 失败,一种都不许退化成"拿 unit 当页号渲一张"。

    两条断言缺一不可:原因**具名**(模型据此知道下一步做什么),以及
    ``pdftoppm`` 压根没被调用过 + 盘面上零 jpeg(证明它真的没进渲染,而不是
    渲了一张然后在信封里说失败)。
    """
    bin_dir = _install_office_stubs(
        tmp_path,
        monkeypatch,
        pdftotext_body=_pdftotext_pages(*pages),
        pdfimages_body=_pdfimages_rows([(2, *_BIG_ROW)]),
    )
    _write_docx(tmp_path / "d.docx", *body)

    env = _run_render(tmp_path, "d.docx", units=units, out_rel=".tool_results/r1/figures/s")

    assert env["ok"] is False, scenario
    assert env["error"] == "render_failed", scenario
    assert [item["why"] for item in env["failed"]] == [why], scenario
    assert _pdftoppm_was_called(bin_dir) is False, f"{scenario}:不该进渲染"
    assert _rendered_jpegs(tmp_path) == [], scenario


def test_docx_anchor_failures_carry_the_anchor_text_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """锚点类失败要把**平台用来定位的那段文字**原样带回去 —— 模型能在
    ``read_document`` 的正文里找到它,据此判断是编号给错了还是排版变了。"""
    _install_office_stubs(
        tmp_path,
        monkeypatch,
        pdftotext_body=_pdftotext_pages("Nothing matching here at all."),
        pdfimages_body=_pdfimages_rows([]),
    )
    _write_docx(tmp_path / "d.docx", _para(_ANCHOR_ONE), _figure())

    env = _run_render(tmp_path, "d.docx", units=[2], out_rel=".tool_results/r1/figures/s")

    assert env["failed"][0]["anchor"] == _ANCHOR_ONE


def test_docx_without_the_page_resolvers_is_a_clean_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """沙箱里没有 pdftotext/pdfimages 时只能猜页 —— 而猜页正是这个功能要消灭的
    东西,所以直接失败,并点名缺了哪个。

    回修第 1 轮 M-4 —— kind 与 ``soffice_missing`` **分开**:这一档换成同一份
    文档的 PDF 版就能绕过去(PDF 路径根本不用这两件工具),缺 soffice 那一档
    换格式也没用,下一步动作不同就不能共用一句话。"""
    bin_dir = _install_office_stubs(tmp_path, monkeypatch)
    (bin_dir / "pdfimages").unlink()
    _write_docx(tmp_path / "d.docx", _para(_ANCHOR_ONE), _figure())
    # PATH 只留桩目录,免得撞上真机上装了的 poppler。
    monkeypatch.setenv("PATH", str(bin_dir))

    env = _run_render(tmp_path, "d.docx", units=[2], out_rel=".tool_results/r1/figures/s")

    assert env["ok"] is False
    assert env["error"] == "docx_resolver_missing"
    assert env["detail"] == "pdfimages"


def test_pdf_input_does_not_need_soffice(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``.pdf`` 输入一次都不调 soffice,不该被那道闸拦下(回修第 2 轮 Minor-3)。

    这道闸原来是无条件的:沙箱里没装 soffice 时,连纯 PDF 也会回
    ``soffice_missing``。M-4 那条"缺 soffice 换格式也没用"的立论正是建在这上面 ——
    它当时只是**恰好**为真。收窄成只在真要转换时才检查,那句话才真的成立。
    """
    bin_dir = _install_office_stubs(tmp_path, monkeypatch)
    (bin_dir / "soffice").unlink()
    (tmp_path / "d.pdf").write_text("fake pdf bytes")
    monkeypatch.setenv("PATH", str(bin_dir))

    env = _run_render(tmp_path, "d.pdf", units=[3], out_rel=".tool_results/r1/figures/s")

    assert env["ok"] is True, f"纯 PDF 被 soffice 闸拦下了:{env}"
    assert env["rendered"][0]["rel"].endswith("page-03.jpg")


def test_docx_that_cannot_be_parsed_is_a_named_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """段落结构读不出来(文件损坏)与"这处图不在第几页"是两件事,不能长得一样。"""
    _install_office_stubs(tmp_path, monkeypatch)
    (tmp_path / "d.docx").write_bytes(b"not a zip at all")

    env = _run_render(tmp_path, "d.docx", units=[2], out_rel=".tool_results/r1/figures/s")

    assert env == {"ok": False, "error": "docx_inventory_failed"}


def test_wrapper_renders_jpeg_not_png() -> None:
    code = build_render_wrapper(
        "d.pptx", units=[1], ws="/workspace", out_rel=".tool_results/r1/figures/s", dpi=RENDER_DPI
    )
    assert "-jpeg" in code
    assert "-png" not in code


def test_require_units_deduplicates_preserving_order() -> None:
    """回修第 3 轮 Important-2 —— ``units=[3, 3]`` 不去重的话,沙箱片段会对同
    一个 ``_u3`` 目录渲两遍:第一遍成功、把 rel 记进 ``rendered``;第二遍对
    同一个目录 rmtree,把第一遍的产物删了,这次万一失败,``rendered`` 里躺的
    就是一条指向已被删除的文件的死 ref(回修第 2 轮 New-2 的 rmtree 修法引入
    的新回归,实测撞到)。去重必须保序,且不能误伤非重复的 unit。

    回修第 4 轮 New-M3 —— "保序"那半条原来咬不住:两组输入(``[3, 3]`` 与
    ``[1, 2, 1, 3, 2]``)的首次出现顺序**恰好都是升序**,``sorted(set(units))``
    这个破坏顺序的去重跑出来逐字相同,变异之后 42 条测试全绿。换一组首次出现
    顺序非升序的输入才分得开这两种实现。"""
    assert _require_units({"units": [3, 3]}) == [3]
    assert _require_units({"units": [5, 1, 5, 2, 1]}) == [5, 1, 2]


def test_spec_is_not_read_only() -> None:
    """回修 I2 —— 这个工具会 makedirs+落中间产物,不是只读。``is_read_only=True``
    会让 L.L6 调度器把两次并发的 read_page 调用当成互不冲突(scheduling.py
    规则 1:只读工具永不冲突),放大 C2 的竞态窗口。"""
    spec = ReadPageTool(client=RecordingSandboxRuntime()).spec
    assert spec.is_read_only is False
    assert spec.side_effect == "reversible"


def test_spec_description_derives_renderable_formats_from_the_single_source() -> None:
    """回修第 2 轮 New-1 —— 模型每一轮都读到的工具描述,不能是一份独立手写的
    格式名单:``_reject_unrenderable_format`` 已经从 ``RENDERABLE_EXTENSIONS``
    派生,描述文案也必须从同一个集合插值,不能手写"PDF"/"PPTX" 这几个字。
    否则 Task 4b 给 RENDERABLE_EXTENSIONS 加 docx 之后,模型读到的描述仍然说
    docx 会被拒——子集钉与身份钉都管不到 prose。"""
    description = ReadPageTool(client=RecordingSandboxRuntime()).spec.description
    for ext in RENDERABLE_EXTENSIONS:
        assert ext.upper() in description
    # 也不能点名"谁被拒"——那是另一份手写名单,同一个坑换个位置。
    # 回修第 3 轮 Minor-1 —— 这里原先是 `assert "DOCX" not in description` /
    # `assert "XLSX" not in description`,本身就是第二份手写格式名单:给
    # RENDERABLE_EXTENSIONS 加 "docx" 后,描述会正确地把 DOCX 挪进正面清单,
    # 这两行会为错误的理由变红(它们守的是"docx 不该出现",而不是"被拒的格式
    # 不该出现")。改成从 RENDERABLE_EXTENSIONS 派生的差集——谁被拒完全跟着
    # 单一真源走。
    for ext in SUPPORTED_EXTENSIONS - RENDERABLE_EXTENSIONS:
        assert ext.upper() not in description

    # Task 4b —— 只钉"被拒的格式不出现"咬不住 prose 里**第二次**点名一个
    # **可渲染**的格式:那同样是一份手写名单,同样会漂。实测撞到:讲 units
    # 含义的那句话曾经写成 "page/slide numbers for PDF and PPTX; for DOCX …",
    # 上面两个循环一条都不红。每个格式在整段描述里最多出现一次,而那一次只可能
    # 来自派生出来的那份清单。
    for ext in SUPPORTED_EXTENSIONS:
        assert description.count(ext.upper()) == (1 if ext in RENDERABLE_EXTENSIONS else 0), ext


# ---------------------------------------------------------------------------
# _validate_rendered_rel —— 回修 I1:参数化的敌对输入 + M2 的扩展名闸。
# 这一层校验的是"沙箱只报告了它自己 exec 视图下的东西"(相对沙箱视图,不是
# 用户根),所以 C1 允许 agents/<key>/... 成为合法 **ref** 字符串不影响这里 ——
# 沙箱片段自己的 exec 视图已经被 mount namespace 关在正确的作用域里,合法的
# rel 永远不会带 agents//shared/ 前缀;出现就是片段本身出了问题,必须拒。
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_rel",
    [
        "../../../../etc/passwd.jpg",
        "/etc/passwd.jpg",
        "agents/other/x.jpg",
        "shared/x.jpg",
        "uploads/x.jpg",
        ".tool_results/../uploads/x.jpg",
        ".tool_resultsX/x.jpg",
        ".tool_results/r1/figures/abc/page-01.exe",
    ],
)
def test_validate_rendered_rel_rejects_adversarial_input(bad_rel: str) -> None:
    assert _validate_rendered_rel(bad_rel) is None


def test_validate_rendered_rel_accepts_a_legitimate_rel() -> None:
    rel = ".tool_results/r1/figures/abc/page-03.jpg"
    assert _validate_rendered_rel(rel) == rel


def test_validated_sandbox_rel_lands_correctly_after_agent_scope_translation() -> None:
    """回修第 1 轮关切 1(裁定:``_validate_rendered_rel`` 保持校验翻译前的
    沙箱侧 rel,不要在这里第二次认 ``agents/<key>/`` —— 那是
    ``workspace_scope.scope_parts`` 的活,抄第二份就是本仓的「两份字面量」
    老病)。这条测试钉住的是**组合**:一个过了 ``_validate_rendered_rel``
    这道闸的沙箱侧 rel,经 ``store_scope`` + ``scoped_path`` 翻译之后,必须
    老老实实落在 ``agents/<key>/.tool_results/...`` 里 —— 不越界、不撞
    ``agents``/``shared`` 保留段,翻译层自己的两道闸(``normalize_workspace_path``
    + ``_reject_reserved_head``)不会把这条本来合法的路径反而拒了。
    """
    sandbox_rel = ".tool_results/r1/figures/abc/page-03.jpg"
    safe_rel = _validate_rendered_rel(sandbox_rel)
    assert safe_rel is not None

    scope = store_scope("/workspace", agent_key="pf-probe-33086dc0")
    host_rel = scoped_path(scope, safe_rel)

    assert host_rel == "agents/pf-probe-33086dc0/.tool_results/r1/figures/abc/page-03.jpg"


# ---------------------------------------------------------------------------
# workspace_figure_ref —— 回修 I5(figure_ref 已删除,换成这个不猜补零的版本)
# ---------------------------------------------------------------------------


def test_workspace_figure_ref_just_joins_the_already_computed_rel() -> None:
    t, u = uuid4(), uuid4()
    rel = "agents/pf-probe-33086dc0/.tool_results/r1/figures/abc/page-005.jpg"
    ref = workspace_figure_ref(t, u, rel)
    assert ref == f"expert_work://workspace/{t}/{u}/{rel}"


# ---------------------------------------------------------------------------
# _doc_sha —— 回修 I6:对 ws + rel 求哈希,不是只对 rel。
# ---------------------------------------------------------------------------


def test_doc_sha_depends_on_ws_not_just_rel() -> None:
    assert _doc_sha("/workspace", "d.pptx") != _doc_sha("/workspace/shared", "d.pptx")


# ---------------------------------------------------------------------------
# ReadPageTool.call
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_too_many_units_is_refused_with_a_reason() -> None:
    runtime = RecordingSandboxRuntime(
        SandboxOutcome(stdout="{}", stderr="", exit_code=0, timed_out=False)
    )
    tool = ReadPageTool(client=runtime)
    result = await tool.call(
        {"path": "d.pptx", "units": list(range(1, MAX_PAGES_PER_CALL + 5))}, ctx=_ctx()
    )
    assert "一次最多" in result.content
    assert result.state_updates.get("viewed_figures", []) == []
    assert runtime.execs == []


@pytest.mark.anyio
async def test_units_at_the_cap_is_not_refused() -> None:
    """M1 —— 边界:``len(units) == MAX_PAGES_PER_CALL`` 必须走通到沙箱,不能被
    ``>`` 误写成 ``>=`` 也测不出来。"""
    runtime = RecordingSandboxRuntime(
        SandboxOutcome(
            stdout=json.dumps({"ok": True, "rendered": []}), stderr="", exit_code=0, timed_out=False
        )
    )
    result = await ReadPageTool(client=runtime).call(
        {"path": "d.pptx", "units": list(range(1, MAX_PAGES_PER_CALL + 1))}, ctx=_ctx()
    )
    assert "一次最多" not in result.content
    assert len(runtime.execs) == 1


@pytest.mark.anyio
async def test_duplicate_units_do_not_eat_the_page_budget() -> None:
    """末轮 M-a —— ``_require_units`` 去重**唯一**真实的理由,是行为级的。

    ``[1, 1, 2, 2]`` 字面是 4 项、超过 ``MAX_PAGES_PER_CALL == 3``,但它实际只要
    两页,必须放行。M-4 当初给这层去重写的第二条理由(「页号播报要报对」)经实测
    是假的 —— 播报由 ``build_render_wrapper`` 自己那道去重保障,把这一层拿掉也
    只报一次。所以这条测试守的是**剩下那条真理由**,而在它之前,两条理由一条
    都没有行为级测试咬住。
    """
    runtime = RecordingSandboxRuntime(
        SandboxOutcome(
            stdout=json.dumps({"ok": True, "rendered": []}), stderr="", exit_code=0, timed_out=False
        )
    )
    result = await ReadPageTool(client=runtime).call(
        {"path": "d.pptx", "units": [1, 1, 2, 2]}, ctx=_ctx()
    )
    assert "一次最多" not in result.content
    assert len(runtime.execs) == 1


@pytest.mark.anyio
async def test_rendered_pages_become_refs_in_state() -> None:
    runtime = RecordingSandboxRuntime(
        SandboxOutcome(
            stdout=json.dumps(
                {
                    "ok": True,
                    "rendered": [
                        {
                            "unit": 3,
                            "rel": ".tool_results/r1/figures/abc/page-03.jpg",
                            "bytes": 61000,
                        }
                    ],
                }
            ),
            stderr="",
            exit_code=0,
            timed_out=False,
        )
    )
    result = await ReadPageTool(client=runtime).call({"path": "d.pptx", "units": [3]}, ctx=_ctx())
    refs = result.state_updates["viewed_figures"]
    assert len(refs) == 1
    assert refs[0].startswith("expert_work://workspace/")
    assert "page-03.jpg" in refs[0]
    assert result.meta["pixel_budget_per_run"] > 0  # 回修 I7 —— 新键名


@pytest.mark.anyio
async def test_rendered_pages_become_agent_scoped_refs_when_agent_bound() -> None:
    """回修 C1 —— 绑了 agent 的 run,ref 里必须带 ``agents/<key>/`` 前缀:沙箱
    视图相对的 rel 直接拼进 ref 会让 ``NasWorkspaceImageResolver`` 按用户根
    ``openat`` 找不到文件(见模块 docstring),而这里已经报了成功。"""
    runtime = RecordingSandboxRuntime(
        SandboxOutcome(
            stdout=json.dumps(
                {
                    "ok": True,
                    "rendered": [
                        {
                            "unit": 3,
                            "rel": ".tool_results/r1/figures/abc/page-03.jpg",
                            "bytes": 61000,
                        }
                    ],
                }
            ),
            stderr="",
            exit_code=0,
            timed_out=False,
        )
    )
    ctx = _ctx(agent_key="pf-probe-33086dc0")
    result = await ReadPageTool(client=runtime).call({"path": "d.pptx", "units": [3]}, ctx=ctx)
    ref = result.state_updates["viewed_figures"][0]
    assert "/agents/pf-probe-33086dc0/.tool_results/r1/figures/abc/page-03.jpg" in ref


@pytest.mark.anyio
async def test_partial_failure_is_surfaced_per_unit() -> None:
    """回修 I4 —— 请求两页只成功一页时,content 要点名哪一页没取到、为什么,
    不能只字不提地假装只请求了成功的那一页。"""
    runtime = RecordingSandboxRuntime(
        SandboxOutcome(
            stdout=json.dumps(
                {
                    "ok": True,
                    "rendered": [
                        {
                            "unit": 1,
                            "rel": ".tool_results/r1/figures/abc/page-01.jpg",
                            "bytes": 100,
                        }
                    ],
                    "failed": [{"unit": 5, "why": "not_rendered"}],
                }
            ),
            stderr="",
            exit_code=0,
            timed_out=False,
        )
    )
    result = await ReadPageTool(client=runtime).call(
        {"path": "d.pptx", "units": [1, 5]}, ctx=_ctx()
    )
    assert "第 5 页没取到" in result.content


@pytest.mark.anyio
async def test_rel_rejected_by_validate_rendered_rel_is_not_silently_swallowed() -> None:
    """回修第 2 轮 New-3 —— 沙箱回报两页, 其中一页的 rel 没过
    ``_validate_rendered_rel`` 这道闸(越权路径,像片段被攻破或出了 bug),
    ``call()`` 此前直接 ``continue`` 把它整条丢弃,content 对模型只字不提 ——
    模型请求了 2 页,只被告知拿到了 1 页,看不出另一页发生了什么。"""
    runtime = RecordingSandboxRuntime(
        SandboxOutcome(
            stdout=json.dumps(
                {
                    "ok": True,
                    "rendered": [
                        {
                            "unit": 1,
                            "rel": ".tool_results/r1/figures/abc/page-01.jpg",
                            "bytes": 100,
                        },
                        {
                            "unit": 2,
                            "rel": "../../etc/passwd.jpg",
                            "bytes": 100,
                        },
                    ],
                }
            ),
            stderr="",
            exit_code=0,
            timed_out=False,
        )
    )
    result = await ReadPageTool(client=runtime).call(
        {"path": "d.pptx", "units": [1, 2]}, ctx=_ctx()
    )
    assert len(result.state_updates["viewed_figures"]) == 1
    assert "第 2 页没取到" in result.content
    assert "不合法" in result.content
    assert len(result.state_updates["viewed_figures"]) == 1


@pytest.mark.anyio
async def test_rejected_rel_with_a_non_int_unit_is_still_reported() -> None:
    """回修第 3 轮 Minor-2 —— 上一条 New-3 的回归钉传的 unit 是 int;这里换成
    沙箱回一个字符串形态的 unit(片段被攻破或出了 bug 都可能出现这种形状),
    证明补救不是只挑 ``isinstance(unit, int)`` 那一支——``_describe_failed_units``
    只是把 unit 塞进 f-string,任何类型都能渲成一句话。"""
    runtime = RecordingSandboxRuntime(
        SandboxOutcome(
            stdout=json.dumps(
                {
                    "ok": True,
                    "rendered": [
                        {
                            "unit": 1,
                            "rel": ".tool_results/r1/figures/abc/page-01.jpg",
                            "bytes": 100,
                        },
                        {"unit": "2", "rel": "../../etc/passwd.jpg", "bytes": 100},
                    ],
                }
            ),
            stderr="",
            exit_code=0,
            timed_out=False,
        )
    )
    result = await ReadPageTool(client=runtime).call(
        {"path": "d.pptx", "units": [1, 2]}, ctx=_ctx()
    )
    assert len(result.state_updates["viewed_figures"]) == 1
    assert "第 2 页没取到" in result.content
    assert "不合法" in result.content


@pytest.mark.anyio
async def test_malformed_rendered_items_are_not_silently_dropped() -> None:
    """回修第 4 轮 New-M1 —— ``rendered`` 里不是 Mapping 的元素原来被裸
    ``continue`` 丢掉,一个字都不记账:请求 3 页、沙箱回 1 张图 + 两个
    ``"bogus"`` 时,模型读到的是"已渲染 d.pptx 第 1 页,已放进你的上下文",
    另外两页只字不提 —— 与 New-3/Minor-2 花力气堵的那条静默失效逐字同形。"""
    runtime = RecordingSandboxRuntime(
        SandboxOutcome(
            stdout=json.dumps(
                {
                    "ok": True,
                    "rendered": [
                        {
                            "unit": 1,
                            "rel": ".tool_results/r1/figures/abc/page-01.jpg",
                            "bytes": 100,
                        },
                        "bogus",
                        "bogus",
                    ],
                }
            ),
            stderr="",
            exit_code=0,
            timed_out=False,
        )
    )
    result = await ReadPageTool(client=runtime).call(
        {"path": "d.pptx", "units": [1, 2, 3]}, ctx=_ctx()
    )
    assert len(result.state_updates["viewed_figures"]) == 1
    assert result.content.count("格式不合法") == 2


@pytest.mark.anyio
async def test_the_fallback_wording_reports_a_count_not_a_page_number() -> None:
    """回修第 4 轮 New-M2 —— 读不出页号时的兜底值是**条数**,原来却被插进
    "已渲染 … 第 {pages} 页"这句**页码**文案里。实测:沙箱回 ``unit:"7"`` 与
    ``unit:"8"``(字符串形态)加两条合法 rel,模型被告知"已渲染 d.pptx 第 2
    页",而上下文里躺的是第 7、8 页 —— 一个具体而错误的页码比不报页码坏得多。
    """
    runtime = RecordingSandboxRuntime(
        SandboxOutcome(
            stdout=json.dumps(
                {
                    "ok": True,
                    "rendered": [
                        {
                            "unit": "7",
                            "rel": ".tool_results/r1/figures/abc/page-07.jpg",
                            "bytes": 100,
                        },
                        {
                            "unit": "8",
                            "rel": ".tool_results/r1/figures/abc/page-08.jpg",
                            "bytes": 100,
                        },
                    ],
                }
            ),
            stderr="",
            exit_code=0,
            timed_out=False,
        )
    )
    result = await ReadPageTool(client=runtime).call(
        {"path": "d.pptx", "units": [7, 8]}, ctx=_ctx()
    )
    assert len(result.state_updates["viewed_figures"]) == 2
    assert "第 2 页" not in result.content
    assert "2 页" in result.content


@pytest.mark.anyio
async def test_malformed_failed_items_are_not_silently_dropped() -> None:
    """回修第 5 轮 I-2 —— ``failed`` 里不是 Mapping 的元素也要说话。

    这是 New-M1(``rendered`` 侧的裸 ``continue``)**同一条通路的另一半**:沙箱回
    ``["bogus", None, 7, {"unit": 3, ...}]`` 时,模型此前只读到第 3 页那一条,
    另外三条一个字不提。"""
    runtime = RecordingSandboxRuntime(
        SandboxOutcome(
            stdout=json.dumps(
                {
                    "ok": True,
                    "rendered": [
                        {
                            "unit": 1,
                            "rel": ".tool_results/r1/figures/abc/page-01.jpg",
                            "bytes": 100,
                        }
                    ],
                    "failed": ["bogus", None, 7, {"unit": 3, "why": "not_rendered"}],
                }
            ),
            stderr="",
            exit_code=0,
            timed_out=False,
        )
    )
    result = await ReadPageTool(client=runtime).call(
        {"path": "d.pptx", "units": [1, 3]}, ctx=_ctx()
    )
    assert "第 3 页没取到" in result.content
    assert result.content.count("失败记录格式不合法") == 3


@pytest.mark.anyio
async def test_a_failed_list_that_is_not_a_list_is_still_reported() -> None:
    """回修第 5 轮 I-2 —— ``failed`` 整个不是列表(沙箱回了个 dict)时,原来
    ``call()`` 一句 ``if isinstance(raw, list) else []`` 就把整块失败信息吃掉了,
    模型读到的是一次干净的成功。"""
    runtime = RecordingSandboxRuntime(
        SandboxOutcome(
            stdout=json.dumps(
                {
                    "ok": True,
                    "rendered": [
                        {
                            "unit": 1,
                            "rel": ".tool_results/r1/figures/abc/page-01.jpg",
                            "bytes": 100,
                        }
                    ],
                    "failed": {"unit": 3, "why": "not_rendered"},
                }
            ),
            stderr="",
            exit_code=0,
            timed_out=False,
        )
    )
    result = await ReadPageTool(client=runtime).call(
        {"path": "d.pptx", "units": [1, 3]}, ctx=_ctx()
    )
    assert "失败清单整体格式不合法" in result.content


@pytest.mark.anyio
async def test_malformed_failed_list_is_reported_on_the_error_path_too() -> None:
    """回修第 5 轮 I-2 —— ``ok: False`` 那条路也要过同一道归一化,两个调用点不能
    只修一个(本任务已经三次栽在"只修证据指的那一处"上)。"""
    runtime = RecordingSandboxRuntime(
        SandboxOutcome(
            stdout=json.dumps({"ok": False, "error": "render_failed", "failed": "bogus"}),
            stderr="",
            exit_code=0,
            timed_out=False,
        )
    )
    result = await ReadPageTool(client=runtime).call({"path": "d.pptx", "units": [1]}, ctx=_ctx())
    assert "失败清单整体格式不合法" in result.content


@pytest.mark.anyio
async def test_soffice_missing_is_undetermined_not_empty() -> None:
    runtime = RecordingSandboxRuntime(
        SandboxOutcome(
            stdout=json.dumps({"ok": False, "error": "soffice_missing"}),
            stderr="",
            exit_code=0,
            timed_out=False,
        )
    )
    result = await ReadPageTool(client=runtime).call({"path": "d.pptx", "units": [1]}, ctx=_ctx())
    assert "无法" in result.content
    assert result.state_updates.get("viewed_figures", []) == []


@pytest.mark.anyio
async def test_not_found_error_gets_an_actionable_chinese_explanation() -> None:
    """回修 I3 —— path 打错字与 LibreOffice 坏了对模型不能长得一样。"""
    runtime = RecordingSandboxRuntime(
        SandboxOutcome(
            stdout=json.dumps({"ok": False, "error": "not_found"}),
            stderr="",
            exit_code=0,
            timed_out=False,
        )
    )
    result = await ReadPageTool(client=runtime).call({"path": "d.pptx", "units": [1]}, ctx=_ctx())
    assert "没有这个文件" in result.content


@pytest.mark.anyio
async def test_error_detail_is_not_dropped() -> None:
    """回修 I3 —— 片段算出来的 detail 不能被工具侧丢掉。"""
    runtime = RecordingSandboxRuntime(
        SandboxOutcome(
            stdout=json.dumps({"ok": False, "error": "convert_failed", "detail": "TimeoutExpired"}),
            stderr="",
            exit_code=0,
            timed_out=False,
        )
    )
    result = await ReadPageTool(client=runtime).call({"path": "d.pptx", "units": [1]}, ctx=_ctx())
    assert "TimeoutExpired" in result.content


# ---------------------------------------------------------------------------
# 回修 C3 —— 扩展名分档:可渲染的格式直通,其余一律在 Python 侧拒绝,不进沙箱
# (runtime.execs == [] 证明零沙箱开销)。
# 回修第 1 轮 M-1:这两句原来写「xlsx/docx/未知」,Task 4b 之后 docx 已经不被
# 拒了 —— 注释不跟着改就是又一份会漂的手写名单。
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_xlsx_is_refused_before_dispatch() -> None:
    runtime = RecordingSandboxRuntime()
    result = await ReadPageTool(client=runtime).call({"path": "s.xlsx", "units": [1]}, ctx=_ctx())
    assert "xlsx" in result.content
    assert "read_document" in result.content
    assert runtime.execs == []


@pytest.mark.anyio
async def test_docx_is_no_longer_refused_by_the_format_gate() -> None:
    """Task 4b —— 这条以前叫 ``..._is_refused_for_now_with_a_temporary_caveat``,
    钉的是 Task 4 的 C3 临时闸。段落序号 -> 页号的解析接上之后,那道闸必须拆掉:
    docx 进沙箱、走正常的渲染路径。"""
    runtime = RecordingSandboxRuntime(
        SandboxOutcome(
            stdout=json.dumps({"ok": True, "rendered": []}), stderr="", exit_code=0, timed_out=False
        )
    )
    result = await ReadPageTool(client=runtime).call({"path": "d.docx", "units": [1]}, ctx=_ctx())
    assert "不支持" not in result.content
    assert len(runtime.execs) == 1


@pytest.mark.anyio
async def test_docx_content_names_both_the_figure_number_and_the_page() -> None:
    """docx 的 ``unit`` 是图清单里的编号,不是页号 —— 只说 "第 7 页" 会给模型
    一个**不存在的页码**(与回修第 4 轮 New-M2 是同一条教训),只说页号又对不上
    它问的那处图。两个都说。"""
    runtime = RecordingSandboxRuntime(
        SandboxOutcome(
            stdout=json.dumps(
                {
                    "ok": True,
                    "rendered": [
                        {
                            "unit": 7,
                            "page": 3,
                            "rel": ".tool_results/r1/figures/abc/page-03.jpg",
                            "bytes": 100,
                        }
                    ],
                }
            ),
            stderr="",
            exit_code=0,
            timed_out=False,
        )
    )
    result = await ReadPageTool(client=runtime).call({"path": "d.docx", "units": [7]}, ctx=_ctx())
    assert "第 7 处(文档第 3 页)" in result.content
    assert "第 7 页" not in result.content


@pytest.mark.anyio
async def test_docx_anchor_only_fallback_is_told_to_the_model() -> None:
    """回落分支渲**成功**了,但那一页不是被图本身钉死的 —— 保留意见必须出现在
    content 里,否则模型会把一张可能不含目标图的页当成确凿证据。"""
    runtime = RecordingSandboxRuntime(
        SandboxOutcome(
            stdout=json.dumps(
                {
                    "ok": True,
                    "rendered": [
                        {
                            "unit": 4,
                            "page": 2,
                            "note": "anchor_only_fallback",
                            "rel": ".tool_results/r1/figures/abc/page-02.jpg",
                            "bytes": 100,
                        }
                    ],
                }
            ),
            stderr="",
            exit_code=0,
            timed_out=False,
        )
    )
    result = await ReadPageTool(client=runtime).call({"path": "d.docx", "units": [4]}, ctx=_ctx())
    assert "文字锚点" in result.content
    assert "下一页" in result.content


@pytest.mark.anyio
async def test_docx_ambiguous_page_fallback_is_told_to_the_model() -> None:
    """回修第 1 轮 C-1 —— 歧义回落那一档必须抵达模型,而且要说清"可能在下一页"。

    这是整条链最容易悄悄断的一节:片段算对了、note 也挂上了,宿主要是不播报,
    模型拿到的仍然是一张"看起来很确定"的页。
    """
    runtime = RecordingSandboxRuntime(
        SandboxOutcome(
            stdout=json.dumps(
                {
                    "ok": True,
                    "rendered": [
                        {
                            "unit": 4,
                            "page": 2,
                            "note": "figure_page_ambiguous_fallback",
                            "rel": ".tool_results/r1/figures/abc/page-02.jpg",
                            "bytes": 100,
                        }
                    ],
                }
            ),
            stderr="",
            exit_code=0,
            timed_out=False,
        )
    )
    result = await ReadPageTool(client=runtime).call({"path": "d.docx", "units": [4]}, ctx=_ctx())
    assert "第 4 处(文档第 2 页)" in result.content
    assert "没法确定哪一张是你要的" in result.content
    assert "再取下一页" in result.content


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("kind", "guess"),
    [
        ("render_failed", "页码超出"),
        ("convert_failed", "可能已损坏"),
        ("docx_inventory_failed", "可能已损坏"),
        # 回修第 4 轮 Minor-2 —— 没登记的 kind 走兜底文案,那句"先怀疑是基础设施
        # 问题"同样是对冲式因果猜测,而它**没有 kind**、永远进不了那个集合。
        ("some_future_kind", "先怀疑"),
    ],
)
async def test_named_per_unit_reasons_are_not_prefixed_by_the_generic_guess(
    kind: str, guess: str
) -> None:
    """有逐条**具名**原因时,不许拿那句对冲式因果猜测开头(回修第 2 轮 A)。

    实测形状:一份带目录页的 docx,所有 unit 都撞 ``anchor_ambiguous``,模型读到的
    第一句却是"大概率是页码超出了文档实际页数",真原因排在它后面 —— 把一个错归因
    摆在真原因前面,与这个 feature 要消灭的东西同形。

    回修第 3 轮 Minor-1 —— 三种 kind 都要钉。``convert_failed`` 与
    ``docx_inventory_failed`` 的"文件可能已损坏"是同一类猜测;它们今天**到不了**
    这条闸(片段里只有 ``render_failed`` 那一处 ``ok: False`` 返回带 ``failed``),
    但"到不了"是**恰好为真**,不是判据。这里从宿主侧直接喂这个形状,把三条都咬住。
    """
    runtime = RecordingSandboxRuntime(
        SandboxOutcome(
            stdout=json.dumps(
                {
                    "ok": False,
                    "error": kind,
                    "failed": [{"unit": 2, "why": "anchor_ambiguous", "anchor": "血糖趋势"}],
                }
            ),
            stderr="",
            exit_code=0,
            timed_out=False,
        )
    )
    result = await ReadPageTool(client=runtime).call({"path": "d.docx", "units": [2]}, ctx=_ctx())
    assert guess not in result.content
    assert "不止出现在一页" in result.content
    assert "血糖趋势" in result.content


@pytest.mark.anyio
async def test_generic_guess_still_shows_when_there_is_no_named_reason() -> None:
    """反面:``failed`` 为空时那句泛化解释仍然要出现 —— 收窄的是"有真原因时别插队",
    不是把它删掉。"""
    runtime = RecordingSandboxRuntime(
        SandboxOutcome(
            stdout=json.dumps({"ok": False, "error": "render_failed"}),
            stderr="",
            exit_code=0,
            timed_out=False,
        )
    )
    result = await ReadPageTool(client=runtime).call({"path": "d.pptx", "units": [99]}, ctx=_ctx())
    assert "页码超出" in result.content


@pytest.mark.anyio
async def test_too_many_units_uses_the_right_counter_word_for_docx() -> None:
    """回修第 1 轮 M-3 —— docx 传的是图清单编号,"你传了 4 页"是在用一个它
    没有的单位说话。量词必须跟着格式走,所以 ``ext`` 得在这句话之前算出来。"""
    runtime = RecordingSandboxRuntime()
    result = await ReadPageTool(client=runtime).call(
        {"path": "d.docx", "units": [1, 2, 3, 4]}, ctx=_ctx()
    )
    assert "你传了 4 处" in result.content
    assert "你传了 4 页" not in result.content
    assert runtime.execs == []


@pytest.mark.anyio
async def test_docx_named_failure_reaches_the_model_with_its_anchor() -> None:
    """四种 honest 失败必须以人话 + 下一步动作抵达模型,并带上平台用来定位的
    那段文字 —— 裸 ``anchor_not_found`` 对模型不构成任何可执行信息。"""
    runtime = RecordingSandboxRuntime(
        SandboxOutcome(
            stdout=json.dumps(
                {
                    "ok": False,
                    "error": "render_failed",
                    "failed": [{"unit": 7, "why": "anchor_not_found", "anchor": "血糖趋势见下图"}],
                }
            ),
            stderr="",
            exit_code=0,
            timed_out=False,
        )
    )
    result = await ReadPageTool(client=runtime).call({"path": "d.docx", "units": [7]}, ctx=_ctx())
    assert "第 7 处没取到" in result.content
    assert "PDF 版" in result.content
    assert "血糖趋势见下图" in result.content


@pytest.mark.anyio
async def test_unknown_extension_is_refused_by_name_before_dispatch() -> None:
    runtime = RecordingSandboxRuntime()
    result = await ReadPageTool(client=runtime).call(
        {"path": "notes.txt", "units": [1]}, ctx=_ctx()
    )
    assert "txt" in result.content
    assert runtime.execs == []


@pytest.mark.anyio
async def test_pdf_and_pptx_are_not_refused_by_the_format_gate() -> None:
    """确认闸只挡不可渲染的格式,不误伤 pdf/pptx 这两种。"""
    for path in ("d.pdf", "d.pptx"):
        runtime = RecordingSandboxRuntime(
            SandboxOutcome(
                stdout=json.dumps({"ok": True, "rendered": []}),
                stderr="",
                exit_code=0,
                timed_out=False,
            )
        )
        result = await ReadPageTool(client=runtime).call({"path": path, "units": [1]}, ctx=_ctx())
        assert "不支持" not in result.content
        assert len(runtime.execs) == 1


# ---------------------------------------------------------------------------
# 回修 I6 —— shared: 现在会被 read_page 拒绝(makedirs 会撞只读 bind)。
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_shared_prefix_is_refused_not_silently_redirected() -> None:
    runtime = RecordingSandboxRuntime()
    with pytest.raises(WriteToSharedError):
        await ReadPageTool(client=runtime).call({"path": "shared:d.pptx", "units": [1]}, ctx=_ctx())
    assert runtime.execs == []
