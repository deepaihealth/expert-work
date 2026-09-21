"""``read_page`` —— 按需把文档单页渲成 jpeg。B-64。

清单(Task 2,``document_figures.py``)只判定「有没有图」,不碰像素;转换真正
的开销推迟到模型确认需要看某一处、调这个工具的那一刻。

**沙箱片段做三件事,顺序不许换**:``soffice`` 转 pdf → ``pdftoppm`` 渲单页
jpeg → glob 找产物。三个实测坑写进片段:

1. soffice 的输出名按源文件 basename 派生 —— 同一个 ``--outdir`` 转两份
   basename 相同、扩展名不同的文档(``x.docx`` / ``x.pptx``)会静默互相覆盖,
   所以每次转换都在**自己的** out_dir 下开一个私有 conv 目录。
2. pdftoppm 把页号补零到**总页数**的宽度(22 页文档的第 11 页是
   ``page-11.jpg``,第 3 页却是 ``page-03.jpg``)—— 调用方不知道总页数,补零
   宽度因此不可预判,只能 glob,不许拼文件名。
3. **glob 的匹配范围必须缩到单个 unit 自己的目录**(回修 C2)—— ``out_dir``
   按 ``(run_id, doc_sha)`` 键、跨调用累积;一个只按 unit 数字做子串匹配的
   glob 会把同一 out_dir 里其它 unit 的残留产物当成这次请求的页(实测撞到:
   先渲第 21 页、再请求第 1 页,``page-*1.jpg`` 这种兜底模式同时命中
   ``page-01.jpg`` 与 ``page-21.jpg``)。每个 unit 因此有自己的私有产出子目
   录,那里面只可能有这个 unit 刚产出的那一个文件。

落点固定在 :data:`~expert_work.persistence.WORKSPACE_OVERFLOW_DIR`
(``.tool_results/``)下,这样渲出来的 ref 才落进
:func:`orchestrator.multimodal.is_cacheable_image_ref` 认的可缓存子树。

**ref 的 ``rel`` 必须是用户根相对,不是沙箱视图相对**(回修 C1)。B-60 之后,
绑了 agent 的 run 在沙箱里看到的 ``/workspace`` bind 的是
``<user_root>/agents/<agent_key>``——沙箱片段回来的 ``rel`` 是相对这个**视图**
算的(``os.path.relpath(got, ws)``),直接拼进 ``expert_work://workspace/...``
ref 会在宿主侧(``NasWorkspaceImageResolver`` 按用户根 openat)少了
``agents/<agent_key>/`` 这一层,读的时候 ``FileNotFoundError``——而这里已经
报了成功,``viewed_figures`` 里躺一条死 ref。翻译用
:mod:`orchestrator.tools.workspace_scope` 这一个唯一真源(``store_scope`` +
``scoped_path``),不手拼路径。
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
from orchestrator.tools.workspace_scope import scoped_path, store_scope

#: 渲染分辨率(dpi)。100 不是 150:``test_render_dpi_keeps_every_page_under_the_vision_limit``
#: 钉了四种常见页型在 100 dpi 下全部 ≤1.15 MP / 长边 ≤1568px(Anthropic 视觉输入线),
#: 150 dpi 全部超线 —— 服务端会把超线的图缩回去,字节多花 2.8 倍换不到更多有效分辨率。
RENDER_DPI: Final = 100
#: 一次调用最多渲几页 —— 硬限,超了直接拒,不进沙箱。
MAX_PAGES_PER_CALL: Final = 3
#: 单个 **run 累计**的像素预算上限(spec §7.2),不是单页(回修 I7 —— 原注释
#: 写反了)。100 dpi 下单页大约 ≤1.15 MP(见上面提到的那条测试),
#: 12,000,000 / 1,150,000 ≈ 10 页量级,与 spec §7.2 "≈12 页 @100 dpi" 同一个
#: 数量级。记在成功结果的 ``meta``(键 ``pixel_budget_per_run``)里,Task 7 的
#: 块渲染据此汇总累计预算;本任务只按页数硬限单次调用,真正的累计追踪不在
#: 这里做。
MAX_RENDER_PIXELS: Final = 12_000_000

_WORKSPACE_ROOT = EXEC_VIEW
#: soffice 转 pdf 的子进程超时。文档偶尔较大,给足预算;真超时时片段吞异常降级成
#: ``convert_failed``,不会挂死沙箱 exec。
_CONVERT_TIMEOUT_S = 90
#: pdftoppm 只渲一页,给一个小得多的超时。
_RENDER_TIMEOUT_S = 30

#: read_page 目前真正能渲染的格式。``unit`` 的含义按格式不同——pptx 是 slide
#: 号、pdf 是页号,两者与 pdftoppm 的页号参数是 1:1 的;docx 是**段落序号**
#: (``document_figures.py`` 的 ``enumerate(paragraphs, 1)``),xlsx 是 sheet
#: 序号,都不能直接喂进 pdftoppm。docx/xlsx 因此显式拒绝而不是当成 pdf/pptx
#: 硬转——那样只会渲出一堆文不对题的页。
_RENDERABLE_EXTENSIONS: Final = frozenset({"pdf", "pptx"})

#: 明确不支持的格式,各自配一句能说清「为什么」的中文理由——两者的"为什么"
#: 不一样,文案不能共用同一句。
#: * ``xlsx`` 是 spec §9.4 的既定结论(不渲染,图表数据走 chart_data)。
#: * ``docx`` 是**临时**限制:段落号 -> 页码的真实映射由另一个任务
#:   (Task 4b,与本任务同一波)接上,这里先按临时闸处理,不是永久结论。
_UNSUPPORTED_FORMAT_REASONS: Final[dict[str, str]] = {
    "xlsx": (
        "xlsx 不支持 read_page —— 这类文档不渲染,图表数据已经在 read_document "
        "返回的文本里,不需要也不该再看图。"
    ),
    "docx": (
        "docx 暂不支持 read_page —— docx 里的编号是段落位置,不是页码,按页码 "
        "取图的映射还没接上。这是临时限制,不代表以后也不支持。"
    ),
}

#: 沙箱回来的 ``{"ok": False, "error": ...}`` 每种 kind 配一句解释 + 下一步,
#: 对齐 ``document_figures.render_figure_map`` 的 undetermined 块那个水准
#: (回修 I3)——不是裸英文枚举。``{detail}`` 是可选的机器细节,不是必须读懂
#: 才能行动的那部分,放在句尾。
_ERROR_EXPLANATIONS: Final[dict[str, str]] = {
    "path_escapes_workspace": "路径越出了你的工作区——检查有没有多余的 '..' 或绝对路径。",
    "not_found": "工作区里没有这个文件——检查路径拼写,可以先用 list_dir 确认它真的在。",
    "io_error": "读这个文件时出了 I/O 错误,不等于文件不存在——重试一次,还不行就换条路径确认。",
    "soffice_missing": "沙箱里没有装转换工具,这不是你能修的问题——先换别的方式获取这处内容。",
    "convert_failed": "文档转 PDF 失败了——文件可能已损坏,或者不是一份合法的这种格式的文件。",
    "render_failed": ("转成 PDF 之后,请求的页一页都没能渲出来——大概率是页码超出了文档实际页数。"),
}
#: 没在上面列出的 kind(理论上不该出现,但沙箱片段变了而这张表没跟上时会
#: 出现)—— 这不等于"这处内容不存在",先怀疑是基础设施问题。
_UNKNOWN_ERROR_EXPLANATION: Final = (
    "原因不明确 —— 这不等于文档里没有这处内容,先怀疑是基础设施问题,换个方式确认。"
)

#: 单个 unit 渲染失败的 ``why`` 配一句人话(回修 I4)。``not_rendered`` 是
#: 片段自己产出的 kind;其余 key 是 ``type(exc).__name__``(如
#: ``TimeoutExpired``),未登记的原样透出 ``why`` 本身当兜底。
_FAILED_UNIT_REASONS: Final[dict[str, str]] = {
    "not_rendered": "转换后没能渲出这一页,常见原因是页码超出了文档实际页数",
    "TimeoutExpired": "渲染这一页超时了",
}


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
    try:
        os.path.getsize(full)
    except FileNotFoundError:
        return {"ok": False, "error": "not_found"}
    except OSError as exc:
        return {"ok": False, "error": "io_error", "detail": str(exc)}
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
    failed = []
    for unit in _P["units"]:
        # 每个 unit 自己的私有产出目录(回修 C2)—— out_dir 跨调用累积, 一个
        # 只按 unit 数字子串匹配的 glob 会把其它 unit 的残留产物错当成这一页。
        # 私有目录里只可能有这个 unit 刚产出的那一个文件, glob 不需要也不许
        # 带 unit 后缀约束。
        unit_dir = os.path.join(out_dir, "_u" + str(unit))
        os.makedirs(unit_dir, exist_ok=True)
        prefix = os.path.join(unit_dir, "page")
        try:
            subprocess.run(
                ["pdftoppm", "-jpeg", "-r", str(_P["dpi"]),
                 "-f", str(unit), "-l", str(unit), pdf, prefix],
                capture_output=True, timeout=_P["render_timeout_s"], check=False)
        except Exception as exc:
            failed.append({"unit": unit, "why": type(exc).__name__})
            continue
        # pdftoppm 把页号补零到总页数的宽度(22 页 -> page-01.jpg), 所以必须
        # glob, 不许拼文件名。
        hits = sorted(_glob.glob(prefix + "-*.jpg"))
        if not hits:
            failed.append({"unit": unit, "why": "not_rendered"})
            continue
        got = hits[-1]
        rendered.append({"unit": unit,
                         "rel": os.path.relpath(got, _P["ws"]),
                         "bytes": os.path.getsize(got)})
    if not rendered:
        return {"ok": False, "error": "render_failed", "failed": failed}
    return {"ok": True, "rendered": rendered, "failed": failed}


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


def workspace_figure_ref(tenant_id: UUID, user_id: UUID, rel: str) -> str:
    """把一个**已经算好的、用户根相对**的 ``rel`` 拼成工作区图片 ref(回修 I5)。

    不做任何路径推断或补零假设 —— ``rel`` 必须是调用方已经校验 + 翻译好的值
    (``ReadPageTool.call`` 用 ``_validate_rendered_rel`` 校验、再用
    ``workspace_scope.scoped_path`` 翻成用户根相对路径之后的那个值)。

    这里原来有一个 ``figure_ref(tenant_id, user_id, run_id, doc_sha, page)``,
    按 ``page-{page:02d}.jpg`` 固定 2 位补零去**猜**渲染管线的产出文件名。它的
    假设本身就被本模块自己的坑 2 推翻了:pdftoppm 按**总页数**补零,<10 页的
    文档产出是 ``page-3.jpg``(不补零),>99 页的文档产出是 ``page-005.jpg``
    (补 3 位),固定 2 位只在 10-99 页这个区间碰巧是对的。它零调用方、零测试
    覆盖(``ReadPageTool.call`` 从来不用它,一直是用沙箱 glob 回来的真实
    ``rel``),所以删掉而不是留着不用 —— 正确结论是这个函数不该存在,不是
    "写对但没人用"。
    """
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


def _reject_unrenderable_format(ext: str) -> str | None:
    """``ext``(不带点、小写)能不能渲染;不能就返回拒绝文案,能就 ``None``。"""
    if ext in _RENDERABLE_EXTENSIONS:
        return None
    reason = _UNSUPPORTED_FORMAT_REASONS.get(ext)
    if reason is not None:
        return reason
    label = f".{ext}" if ext else "(无扩展名)"
    return f"read_page 不支持 {label} 这种格式 —— 目前只能渲染 pdf / pptx。"


def _validate_rendered_rel(rel: object) -> str | None:
    """约束 A —— 沙箱回来的 ``rel``(相对沙箱自己的 exec 视图 ``ws``)必须落在
    ``.tool_results/`` 下、不越权、扩展名是 ``.jpg``(回修 M2)。

    这一层校验的是"沙箱只报告了它自己写出来的东西",与 C1 的用户根翻译是
    两件不同的事、发生在不同的层:沙箱的 exec 视图本身就已经被 B-60 的 mount
    namespace 关在正确的作用域里(绑了 agent 时 ``/workspace`` 就是那个 agent
    自己的目录),``os.path.relpath(got, ws)`` 算出来的 ``rel`` 因此**永远不会**
    合法地带 ``agents/``/``shared/`` 前缀 —— 出现这类前缀只可能是沙箱片段本身
    出了问题(bug 或被攻破),必须拒,而不是因为 C1 之后 ``agents/<key>/...``
    在**ref 字符串**层面变合法了就跟着放行。C1 影响的是下一步
    ``workspace_scope.scoped_path`` 的翻译结果(用户根相对路径,那里才会出现
    ``agents/<key>/...``),不影响这一步的判据。

    校验不过就当这一页渲染失败(返回 ``None``),调用方据此把它从
    ``viewed_figures`` 里剔除 —— 不吞,也不当成功接受一个可能指向别处的 ref。
    """
    if not isinstance(rel, str) or not rel or not rel.endswith(".jpg"):
        return None
    if rel.startswith("/"):
        return None
    if ".." in PurePosixPath(rel).parts:
        return None
    if rel != WORKSPACE_OVERFLOW_DIR and not rel.startswith(f"{WORKSPACE_OVERFLOW_DIR}/"):
        return None
    return rel


def _doc_sha(ws: str, rel: str) -> str:
    """文档身份指纹 —— 决定 ``out_dir`` 的键之一(回修 I6)。

    对 ``ws + "/" + rel`` 求哈希,不是只对 ``rel``:不同作用域(``ws``)下可能
    出现同名的 ``rel``,不该共享同一个 out_dir——``shared:d.pptx`` 与自己目录
    下的 ``d.pptx`` 曾经是同一个 sha(只对 ``rel`` 哈希),对文档 A 的渲染会
    落进对文档 B 已经在用的 out_dir。``read_page`` 现在把 ``shared:`` 挡在了
    ``resolve_scope`` 那一层(见 ``workspace_paths._WRITE_TOOLS``),这里是第二
    层防线:即便以后 ``ws`` 又出现别的取值,同名 rel 也不会撞进同一个目录。
    """
    return hashlib.sha256(f"{ws}/{rel}".encode()).hexdigest()[:16]


def _explain_error(kind: str, detail: object) -> str:
    explanation = _ERROR_EXPLANATIONS.get(kind, _UNKNOWN_ERROR_EXPLANATION)
    return f"{explanation}(detail: {detail})" if detail else explanation


def _describe_failed_units(failed: object) -> str:
    """把片段回来的 ``failed: [{"unit": n, "why": ...}]`` 渲成一句人话(回修 I4)。

    空输入 → 空串(不加多余的句子)。
    """
    if not isinstance(failed, list) or not failed:
        return ""
    parts: list[str] = []
    for item in failed:
        if not isinstance(item, Mapping):
            continue
        unit = item.get("unit")
        why = str(item.get("why", "unknown"))
        reason = _FAILED_UNIT_REASONS.get(why, why)
        parts.append(f"第 {unit} 页没取到({reason})")
    return "".join(f"{part}。" for part in parts)


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
                "Only PDF and PPTX are renderable today; DOCX and XLSX are refused "
                "with an explanation (their unit numbering doesn't map to a page). "
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
            # 回修 I2 —— 这个工具会 makedirs + 落中间 pdf/jpg 到工作区的
            # .tool_results/ 下,不是只读。is_read_only=True 会让 L.L6 的调度
            # 器把两次并发的 read_page 调用当成互不冲突(scheduling.py 规则 1:
            # 只读工具永不冲突),两次调用共享同一个 out_dir 时 C2 的竞态窗口
            # 就被放大了。
            is_read_only=False,
            path_args=("path",),
            side_effect="reversible",
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
        ext = PurePosixPath(raw).suffix.lower().lstrip(".")
        rejection = _reject_unrenderable_format(ext)
        if rejection is not None:
            return ToolResult(content=rejection)
        if ctx.tenant_id is None or ctx.user_id is None or ctx.run_id is None:
            # 约束 B —— 三个 id 缺一个就没法造 workspace ref;说明白,不崩。
            return ToolResult(content="无法渲染:这条 run 缺少租户/用户/run 绑定。")
        ws, rel = resolve_scope(raw, agent_key=ctx.agent_key, tool="read_page")
        doc_sha = _doc_sha(ws, rel)
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
            kind = str(env.get("error", "unknown"))
            msg = f"无法渲染 {raw}:{_explain_error(kind, env.get('detail'))}"
            per_unit = _describe_failed_units(env.get("failed"))
            if per_unit:
                msg = f"{msg} {per_unit}"
            return ToolResult(content=msg)

        # 回修 C1 —— 沙箱回来的 rel 是相对沙箱自己的 exec 视图算的;翻成用户
        # 根相对路径才能造出 NasWorkspaceImageResolver 能解析的 ref。
        scope = store_scope(ws, agent_key=ctx.agent_key)
        refs: list[str] = []
        rendered_units: list[int] = []
        for item in env.get("rendered") or ():
            if not isinstance(item, Mapping):
                continue
            safe_rel = _validate_rendered_rel(item.get("rel"))
            if safe_rel is None:
                continue
            host_rel = scoped_path(scope, safe_rel)
            refs.append(workspace_figure_ref(ctx.tenant_id, ctx.user_id, host_rel))
            unit = item.get("unit")
            if isinstance(unit, int) and not isinstance(unit, bool):
                rendered_units.append(unit)
        if not refs:
            return ToolResult(content=f"无法渲染 {raw}:没有取到任何页。")

        pages = "、".join(str(u) for u in rendered_units) if rendered_units else str(len(refs))
        content = (
            f"已渲染 {raw} 第 {pages} 页,已放进你的上下文 —— 需要仔细看细节时用 ask_image 问它。"
        )
        per_unit = _describe_failed_units(env.get("failed"))
        if per_unit:
            content = f"{content} {per_unit}"
        return ToolResult(
            content=content,
            meta={"pixel_budget_per_run": MAX_RENDER_PIXELS},
            state_updates={"viewed_figures": refs},
        )
