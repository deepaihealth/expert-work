"""``read_page`` —— 按需把文档单页渲成 jpeg。B-64。

清单(Task 2,``document_figures.py``)只判定「有没有图」,不碰像素;转换真正
的开销推迟到模型确认需要看某一处、调这个工具的那一刻。

**沙箱片段做三件事,顺序不许换**:``soffice`` 转 pdf → ``pdftoppm`` 渲单页
jpeg → glob 找产物。两个实测坑写进片段:

1. soffice 的输出名按源文件 basename 派生 —— 同一个 ``--outdir`` 转两份
   basename 相同、扩展名不同的文档(``x.docx`` / ``x.pptx``)会静默互相覆盖,
   所以每次转换都在**自己的** out_dir 下开一个私有 conv 目录。
2. pdftoppm 把页号补零到**总页数**的宽度(22 页文档的第 11 页是
   ``page-11.jpg``,第 3 页却是 ``page-03.jpg``)—— 调用方不知道总页数,补零
   宽度因此不可预判,只能 glob,不许拼文件名。

落点固定在 :data:`~expert_work.persistence.WORKSPACE_OVERFLOW_DIR`
(``.tool_results/``)下,这样渲出来的 ref 才落进
:func:`orchestrator.multimodal.is_cacheable_image_ref` 认的可缓存子树。
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Final
from uuid import UUID

from expert_work.persistence import WORKSPACE_OVERFLOW_DIR
from expert_work.protocol.multimodal import WORKSPACE_REF_PREFIX
from orchestrator.tools.file_ops import _require_path, _snippet, run_scoped_read
from orchestrator.tools.registry import ToolContext, ToolResult, ToolSpec
from orchestrator.tools.sandbox import SandboxRuntime
from orchestrator.tools.sandbox_image_contract import EXEC_VIEW
from orchestrator.tools.workspace_paths import resolve_scope

#: 渲染分辨率(dpi)。100 不是 150:``test_render_dpi_keeps_every_page_under_the_vision_limit``
#: 钉了四种常见页型在 100 dpi 下全部 ≤1.15 MP / 长边 ≤1568px(Anthropic 视觉输入线),
#: 150 dpi 全部超线 —— 服务端会把超线的图缩回去,字节多花 2.8 倍换不到更多有效分辨率。
RENDER_DPI: Final = 100
#: 一次调用最多渲几页 —— 硬限,超了直接拒,不进沙箱。
MAX_PAGES_PER_CALL: Final = 3
#: 单页像素预算上限,记在成功结果的 ``meta`` 里(Task 7 的块渲染按 ``viewed_figures``
#: 的条目数汇总累计预算)。本任务只按页数硬限调用;真正按像素计的累计预算不在这里算。
MAX_RENDER_PIXELS: Final = 12_000_000

_WORKSPACE_ROOT = EXEC_VIEW
#: soffice 转 pdf 的子进程超时。文档偶尔较大,给足预算;真超时时片段吞异常降级成
#: ``convert_failed``,不会挂死沙箱 exec。
_CONVERT_TIMEOUT_S = 90
#: pdftoppm 只渲一页,给一个小得多的超时。
_RENDER_TIMEOUT_S = 30


# 沙箱内渲染片段。``os`` / ``json`` / ``_P`` / ``_resolve`` 来自共享的
# ``_PRELUDE``(见 file_ops)。
_RENDER_MAIN = """

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
        # 每次转换自己的 outdir —— soffice 的输出名按 basename 派生, 同目录里
        # x.docx 与 x.pptx 会互相静默覆盖(实测撞到过)。
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
        # pdftoppm 把页号补零到总页数的宽度(22 页 -> page-01.jpg), 所以必须
        # glob, 不许拼文件名。
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
"""


def build_render_wrapper(
    rel: str,
    *,
    units: Sequence[int],
    out_rel: str,
    ws: str = _WORKSPACE_ROOT,
    dpi: int = RENDER_DPI,
    convert_timeout_s: int = _CONVERT_TIMEOUT_S,
    render_timeout_s: int = _RENDER_TIMEOUT_S,
) -> str:
    """沙箱片段:把 ``ws/rel`` 的 ``units`` 页渲成 jpeg,写进 ``ws/out_rel/``。"""
    return _snippet(
        {
            "ws": ws,
            "rel": rel,
            "units": list(units),
            "out_rel": out_rel,
            "dpi": dpi,
            "convert_timeout_s": convert_timeout_s,
            "render_timeout_s": render_timeout_s,
        },
        _RENDER_MAIN,
    )


def figure_ref(tenant_id: UUID, user_id: UUID, run_id: UUID, doc_sha: str, page: int) -> str:
    """按标准落点拼一个工作区图片 ref —— ``.tool_results/<run_id>/figures/<doc_sha>/page-NN.jpg``。

    ``NN`` 固定按 2 位补零。**这是约定名,不是渲染管线真实产出的文件名** ——
    ``ReadPageTool.call`` 不用这个函数拼它写进 ``viewed_figures`` 的 ref,而是
    直接用沙箱片段 glob 到的真实 ``rel``(见该方法的实现与模块头部第 2 条坑):
    pdftoppm 按总页数补零,超过 99 页的文档会补 3 位,这里假设的 2 位在那种
    情况下会拼出一个不存在的文件。这个函数留给能接受"2 位是绝大多数情况"这个
    假设的调用方(例如按约定预判一个 ref 而不必先跑一次渲染)。
    """
    rel = f"{WORKSPACE_OVERFLOW_DIR}/{run_id}/figures/{doc_sha}/page-{page:02d}.jpg"
    return f"{WORKSPACE_REF_PREFIX}{tenant_id}/{user_id}/{rel}"


def _require_units(args: Mapping[str, Any]) -> list[int]:
    raw = args.get("units")
    if not isinstance(raw, list) or not raw:
        msg = "read_page requires a non-empty 'units' list"
        raise ValueError(msg)
    units: list[int] = []
    for value in raw:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            msg = "read_page 'units' must be a list of positive integers"
            raise ValueError(msg)
        units.append(value)
    return units


def _validate_rendered_rel(rel: object) -> str | None:
    """约束 A —— 沙箱回来的 ``rel`` 必须落在 ``.tool_results/`` 下、不越权。

    校验不过就当这一页渲染失败(返回 ``None``),调用方据此把它从
    ``viewed_figures`` 里剔除 —— 不吞,也不当成功接受一个可能指向别处的 ref。
    """
    if not isinstance(rel, str) or not rel or rel.startswith("/"):
        return None
    if ".." in PurePosixPath(rel).parts:
        return None
    if rel != WORKSPACE_OVERFLOW_DIR and not rel.startswith(f"{WORKSPACE_OVERFLOW_DIR}/"):
        return None
    return rel


@dataclass
class ReadPageTool:
    """按需把文档单页渲成 jpeg(暴露为 ``read_page``)。"""

    client: SandboxRuntime
    #: skill-runtime §5.1 — activated skill files seeded under /opt/skills/<agent_key>/.
    skill_seed_files: tuple[tuple[str, bytes], ...] = ()

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="read_page",
            description=(
                "Render a specific page/slide of a document in your own workspace "
                "into an image, so you can actually look at a figure or chart the "
                "document's figure map (shown by read_document) told you about. "
                "'units' are 1-based page/slide numbers; at most "
                f"{MAX_PAGES_PER_CALL} per call — call it again for more. Paths "
                "are relative to your own workspace root."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Workspace-relative document path (no leading '/' or '..').",
                    },
                    "units": {
                        "type": "array",
                        "items": {"type": "integer", "minimum": 1},
                        "description": (
                            f"1-based page/slide numbers to render (max {MAX_PAGES_PER_CALL})."
                        ),
                    },
                },
                "required": ["path", "units"],
            },
            is_read_only=True,
            path_args=("path",),
            side_effect="read_only",
            idempotent=True,
        )

    async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
        raw = _require_path(args, tool="read_page", agent_key=ctx.agent_key)
        units = _require_units(args)
        if len(units) > MAX_PAGES_PER_CALL:
            return ToolResult(
                content=(
                    f"一次最多取 {MAX_PAGES_PER_CALL} 页,你传了 {len(units)} 页 —— "
                    "分几次调用,每次不超过这个数。"
                ),
            )
        if ctx.tenant_id is None or ctx.user_id is None or ctx.run_id is None:
            # 约束 B —— 三个 id 缺一个就没法造 workspace ref;说明白,不崩。
            return ToolResult(content="无法渲染:这条 run 缺少租户/用户/run 绑定。")
        ws, rel = resolve_scope(raw, agent_key=ctx.agent_key, tool="read_page")
        doc_sha = hashlib.sha256(rel.encode()).hexdigest()[:16]
        out_rel = f"{WORKSPACE_OVERFLOW_DIR}/{ctx.run_id}/figures/{doc_sha}"
        env = await run_scoped_read(
            self.client,
            build=lambda w: build_render_wrapper(
                rel, units=units, ws=w, out_rel=out_rel, dpi=RENDER_DPI
            ),
            ws=ws,
            ctx=ctx,
            tool="read_page",
            seed_files=self.skill_seed_files,
        )
        if not env.get("ok"):
            return ToolResult(content=f"无法渲染 {raw}:{env.get('error', 'unknown')}。")

        refs: list[str] = []
        rendered_units: list[int] = []
        for item in env.get("rendered") or ():
            if not isinstance(item, Mapping):
                continue
            safe_rel = _validate_rendered_rel(item.get("rel"))
            if safe_rel is None:
                continue
            refs.append(f"{WORKSPACE_REF_PREFIX}{ctx.tenant_id}/{ctx.user_id}/{safe_rel}")
            unit = item.get("unit")
            if isinstance(unit, int) and not isinstance(unit, bool):
                rendered_units.append(unit)
        if not refs:
            return ToolResult(content=f"无法渲染 {raw}:没有取到任何页。")

        pages = "、".join(str(u) for u in rendered_units) if rendered_units else str(len(refs))
        content = (
            f"已渲染 {raw} 第 {pages} 页,已放进你的上下文 —— 需要仔细看细节时用 ask_image 问它。"
        )
        return ToolResult(
            content=content,
            meta={"pixel_budget_per_page": MAX_RENDER_PIXELS},
            state_updates={"viewed_figures": refs},
        )
