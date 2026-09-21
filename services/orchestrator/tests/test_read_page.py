"""``read_page`` —— 按需渲染文档单页。"""

from __future__ import annotations

import ast
import contextlib
import io
import json
import os
import shutil
from pathlib import Path
from uuid import uuid4

import pytest

from orchestrator.multimodal import is_cacheable_image_ref
from orchestrator.tools.document_figures import RENDERABLE_EXTENSIONS, SUPPORTED_EXTENSIONS
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
    """M5 —— 内层预算(soffice 转换一次 + 最多 MAX_PAGES_PER_CALL 次单页渲染)
    必须严格小于外层 exec 的超时上限,否则一次合法的满页调用会被沙箱供应商
    自己的兜底超时打断,而不是片段自己的 subprocess timeout 优雅降级。"""
    worst_case = _CONVERT_TIMEOUT_S + MAX_PAGES_PER_CALL * _RENDER_TIMEOUT_S
    assert worst_case < _MAX_EXEC_TIMEOUT_S


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
            "#!/usr/bin/env python3\n"
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


def _install_office_stubs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    soffice_body: str = _SOFFICE_WRITES_A_PDF,
    pdftoppm_body: str = _PDFTOPPM_WRITES_A_JPEG,
) -> None:
    """伪造 soffice / pdftoppm 到 PATH 最前面,只让剧本那一段随测试变。

    回修第 4 轮 New-M6 —— 这里原先是五个 helper 各自内联一整份 soffice +
    pdftoppm 桩源码(其中两份 pdftoppm 逐字相同),不影响正确性,但下次改桩
    要改五处。收口成这一个入口之后,变的只有 ``*_body``,两段 preamble 与
    调用计数只有一份。
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    soffice = bin_dir / "soffice"
    soffice.write_text(
        _stub_source(binary="soffice", preamble=_SOFFICE_PREAMBLE, body=soffice_body)
    )
    pdftoppm = bin_dir / "pdftoppm"
    pdftoppm.write_text(
        _stub_source(binary="pdftoppm", preamble=_PDFTOPPM_PREAMBLE, body=pdftoppm_body)
    )
    for script in (soffice, pdftoppm):
        script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")


def _run_render(
    tmp_path: Path, rel: str, *, units: list[int], out_rel: str, dpi: int = RENDER_DPI
) -> dict:
    """本地真跑渲染片段(与 test_document_figures._run 同一房规)。"""
    code = build_render_wrapper(rel, units=units, ws=str(tmp_path), out_rel=out_rel, dpi=dpi)
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
    (tmp_path / "uploads" / "report.docx").write_text("docx source")
    (tmp_path / "uploads" / "report.pptx").write_text("pptx source")

    env_a = _run_render(
        tmp_path, "uploads/report.docx", units=[1], out_rel=".tool_results/r1/figures/aaa"
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
# 回修 C3 —— 扩展名分档:pptx/pdf 直通,xlsx/docx/未知格式一律在 Python 侧
# 拒绝,不进沙箱(runtime.execs == [] 证明零沙箱开销)。
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_xlsx_is_refused_before_dispatch() -> None:
    runtime = RecordingSandboxRuntime()
    result = await ReadPageTool(client=runtime).call({"path": "s.xlsx", "units": [1]}, ctx=_ctx())
    assert "xlsx" in result.content
    assert "read_document" in result.content
    assert runtime.execs == []


@pytest.mark.anyio
async def test_docx_is_refused_for_now_with_a_temporary_caveat() -> None:
    runtime = RecordingSandboxRuntime()
    result = await ReadPageTool(client=runtime).call({"path": "d.docx", "units": [1]}, ctx=_ctx())
    assert "docx" in result.content
    assert "段落" in result.content
    assert "临时" in result.content
    assert runtime.execs == []


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
    """确认闸只挡 xlsx/docx/未知,不误伤真正支持的两种格式。"""
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
