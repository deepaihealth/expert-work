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
4. **私有产出目录必须在渲染前清空**(回修第 2 轮 New-2,回修第 3 轮
   Important-1 扩到 soffice 那一层)—— 3 里的私有目录只挡跨 unit 混淆,挡不
   住"同一个目录被重用、这一次失败了"这种情况:``out_dir`` 跨调用存活,旧
   文件还在目录里;这一次的子进程非零退出、没产新文件,glob 会把上一次的
   旧文件当成这次的产出报成功(实测撞到:同一个 rel 指向的文档在同一个
   run 里被换过内容之后,这条路会把旧文档的页报成新文档的页——一次真实
   的失败被翻译成了成功)。**片段里每一处 ``subprocess.run``、每一处"靠有
   没有产出文件反推成功"的地方都要过这道闸**——不是只堵复审那一轮指出的
   那一行:``soffice``/``_pdf`` 转换目录和 ``pdftoppm``/单 unit 目录各自
   独立,回修第 2 轮只堵了后者,前者原样开着直到第 3 轮才补上。渲染/转换前
   先 ``shutil.rmtree`` 再 ``makedirs``,并且接住 ``subprocess.run`` 的返回
   码,非零直接算失败,不再只靠"有没有产出文件"去反推。
5. **清空本身也可能悄悄没生效**(回修第 3 轮 Minor-3)—— ``shutil.rmtree``
   配 ``ignore_errors=True`` 会吞掉"目标是指向别处的目录符号链接"这类错误
   (rmtree 拒绝 follow 符号链接),而 ``os.makedirs(exist_ok=True)`` 对一个
   符号链接指向的已存在目录不会报错,于是"已经清空"这个前提会悄悄落空,
   4 里的两道闸也就失去了意义。清空 + 重建之后必须再确认
   ``not (islink or listdir)``,不干净就当一次真实失败播报,不能当没看见
   继续往下走。
6. **产出路径必须跟着文件内容走**(回修第 4 轮 New-I1)—— 上面 4、5 两条堵的
   是"同一个目录被重用"带来的各种走样,但它们共享同一个前提:宿主侧的
   ``out_rel`` 只按 ``(run_id, 文档路径)`` 算(见 :func:`_doc_sha`),同一条
   路径换了内容,拿到的是**逐字相同**的产出 rel。于是 ``.tool_results/`` 下
   那条 ref 被 :class:`~orchestrator.multimodal.CachingImageResolver` 记住之
   后,文档被覆盖再渲一次,模型看到的还是旧文档那一页 —— 缓存是进程级、没有
   TTL、没有失效通道,这一层的走样比前两层更难发现。修法是把**文件内容的
   哈希**(片段里算,流式读)拼进产出路径:内容一变,路径就变,缓存与磁盘同时
   自然失效。这句话的边界是 sha256 取前 16 位 hex(64 位)的抗碰撞性,不是别的
   机制。相应地,内容没变时重复调用仍然会重新渲染一遍 —— 这是故意的,"已经存在
   就跳过"会把 4 刚拔掉的竞态原样请回来。

7. **这一刀给下游带来的新形状**(回修第 5 轮 M-5,给 Task 7 留话)—— 同一页的
   **每个内容版本**现在各得一条独立 ref(以前是同一条),于是:

   * :func:`orchestrator.state._merge_viewed_figures` 是**首次出现序 union**,
     所以"编辑文档 → 重读第 3 页"会把**新旧两版**一起推进将来
     ``_figure_block_tail`` 的滑窗,而**没有任何东西标注哪一版是当前的** ——
     模型可能同时看到同一页的两个版本。这比旧行为好(旧行为是直接发旧字节),
     但它是个新形状,不是自动就对。Task 7 做滑窗时要自己决定:是按
     ``(doc-sha, unit)`` 只留最新一版,还是把"这是第几版"显式说给模型。
   * 每个内容版本在 ``.tool_results/<run_id>/`` 下**永久留一个 ``<render-sha>/``
     目录**,直到这条 run 被 purge(B-50 Task 12 的会话 purge + 留存 job 孤儿
     扫描收它)。一条 run 里反复编辑同一份文档会线性堆目录。

8. **docx 的 ``unit`` 是段落序号,渲染前必须解析成页号**(Task 4b)——
   ``document_figures._docx_inventory`` 的 ``enumerate(paragraphs, 1)`` 数的是
   段落,不是页。直接喂进 ``pdftoppm -f/-l`` 会渲出一张无关页**并声称它就是
   模型问的那处图**,所以这条路在 Task 4 的 C3 里是被显式拒掉的。现在接上,
   两步组合,一步都不能少:

   a. **anchor 定位邻域**:``pdftotext <pdf> -`` 一次取回整篇,页间以换页符
      分隔;把页文本与清单里的 ``anchor``(图**正上方**那段文字的尾部 60 字符)
      都做空白归一化再找包含关系。``anchor`` 落在页底时图会被挤到下一页,
      所以这一步只给"含 anchor 的那一页或紧邻的下一页"这个**邻域**,不给答案。
   b. **``pdfimages -list`` 把页钉死**:取邻域内、页号最小的那一行位图所在的页。

   两步是组合不是二选一:只用 a 会在页边界上错一页;只用 b 对不上是**哪一处**
   图(清单跳过了装饰图,序号对不齐)。b 这一步还必须按与清单**同一条**尺寸
   阈值滤掉装饰行 —— 页眉/页脚的 logo 住在 ``word/header1.xml``、清单根本看
   不见它,而它在**每一页**都有一行,不滤的话 b 的结果恒等于 a 的邻域页,
   页边界那一档就永远修不对(实测:同一个 XObject 被 N 页引用会出 N 行)。

   **拿不到答案时一律具名失败,一处都不猜页**:``anchor_empty`` /
   ``anchor_not_found`` / ``anchor_ambiguous`` / ``unit_not_in_inventory``
   四种(见 :data:`_FAILED_UNIT_REASONS`)。邻域内一行位图都没有是**例外**:
   那多半是图表/SmartArt/EMF 被 LibreOffice 渲成了 PDF 矢量算子(``pdfimages``
   看不见矢量),这时回落到邻域页本身,但必须把"这一页是按文字锚点定位的"
   说给模型(见 :data:`_RENDER_NOTE_REASONS`)。

   这条路**不保证**总是对:锚点那一页本身另有一张真图、而目标图被挤到了下一页
   时,b 会停在锚点页。这是已知残留,不是被消灭的情况 —— 见任务报告。

落点固定在 :data:`~expert_work.persistence.WORKSPACE_OVERFLOW_DIR`
(``.tool_results/``)下,这样渲出来的 ref 才落进
:func:`orchestrator.multimodal.is_cacheable_image_ref` 认的可缓存子树。完整形状
是 ``.tool_results/<run_id>/figures/<doc-sha>/<render-sha>/_u<unit>/page-NN.jpg``
—— 前四段由宿主拼(:meth:`ReadPageTool.call` 的 ``out_rel``),后三段由片段拼。

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
from typing import Any, Final, TypeGuard
from uuid import UUID

from expert_work.persistence import (
    RENDERED_FIGURE_DIR,
    RENDERED_FIGURE_PAGE_STEM,
    RENDERED_FIGURE_SHA_HEX_LEN,
    RENDERED_FIGURE_UNIT_PREFIX,
    WORKSPACE_OVERFLOW_DIR,
)
from expert_work.protocol.multimodal import WORKSPACE_REF_PREFIX
from orchestrator.tools.document_figures import (
    _DOCX_INVENTORY_FRAGMENT,
    RENDERABLE_EXTENSIONS,
)
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

#: read_page 目前真正能渲染的格式 —— 单一真源在 ``document_figures.py``
#: (回修第 1 轮关切 2:两处各写一份字面量是本仓已经吃过亏的病,见那边
#: ``RENDERABLE_EXTENSIONS`` 的 docstring)。``unit`` 的含义按格式不同——pptx
#: 是 slide 号、pdf 是页号,两者与 pdftoppm 的页号参数是 1:1 的;docx 是图
#: 清单里的**条目编号**(底下是 ``enumerate(paragraphs, 1)`` 的段落序号),
#: 由片段在渲染前解析成页号(Task 4b,见模块 docstring 第 8 条);xlsx 是
#: sheet 序号,没有对应的页,永远不渲染。

#: 明确不支持的格式,各自配一句能说清「为什么」的中文理由。
#: 今天只剩 ``xlsx``,是 spec §9.4 的既定结论(不渲染,图表数据走 chart_data)。
#: docx 曾经也在这里(段落号喂不进 pdftoppm),Task 4b 把段落号 -> 页号的解析
#: 接上之后它进了 ``RENDERABLE_EXTENSIONS``,这条也就跟着删了。
_UNSUPPORTED_FORMAT_REASONS: Final[dict[str, str]] = {
    "xlsx": (
        "xlsx 不支持 read_page —— 这类文档不渲染,图表数据已经在 read_document "
        "返回的文本里,不需要也不该再看图。"
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
    #: Task 4b —— docx 专有的三种基础设施失败。共同点是"定位不了这处图在第几页",
    #: 所以都给同一条替代路径(换 PDF 版),但各自的病因不一样,不共用一句。
    "docx_inventory_failed": (
        "读这份 docx 的段落结构失败了,定位不了图在第几页——文件可能已损坏,或者不是一份合法的 docx。"
    ),
    "pdftotext_failed": (
        "文档转成 PDF 了,但取不出每页的文字,就没法按文字锚点定位这处图在第几页"
        "——换成同一份文档的 PDF 版再试,那种格式的编号本来就是页号。"
    ),
    "pdfimages_failed": (
        "文档转成 PDF 了,但列不出页面里的图,就没法把这处图的页号钉死"
        "——换成同一份文档的 PDF 版再试,那种格式的编号本来就是页号。"
    ),
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
    #: 回修第 2 轮 New-2 —— pdftoppm 非零退出(接住返回码,不再只靠有没有
    #: 产出文件去反推)。
    "pdftoppm_failed": "pdftoppm 处理这一页失败了",
    #: 回修第 2 轮 New-3 —— 沙箱回报的 rel 没过 _validate_rendered_rel 这道
    #: 闸(见该函数 docstring),不能被 call() 悄悄吞掉。
    "rejected_rel": "产出路径不合法,已丢弃",
    #: 回修第 3 轮 Minor-3 —— 私有产出目录清空后仍不干净(符号链接被
    #: rmtree 悄悄跳过,或残留文件删不掉),不能当没看见继续往下走。
    "unit_dir_not_clean": "这一页的临时产出目录没能清空,为安全起见跳过了",
    #: 回修第 4 轮 New-M1 —— ``rendered`` 里出现了不是 Mapping 的元素。原来
    #: 直接 ``continue`` 丢弃,模型对这一条只字不提,与前面花力气堵的那条静默
    #: 失效逐字同形。这种形状下连它是第几页都读不出来,所以 ``unit`` 记成
    #: ``"?"``,但"有东西被丢了"这件事必须说出来。
    "malformed_item": "这一条产出记录格式不合法,已丢弃",
    #: 回修第 5 轮 I-2 —— ``failed`` 里出现了不是 Mapping 的元素。与
    #: ``malformed_item`` 同形,只是落在失败清单这一侧。
    "malformed_failed_item": "这一条失败记录格式不合法",
    #: 回修第 5 轮 I-2 —— ``failed`` 整个就不是一个列表(沙箱回了 dict / 字符串
    #: 之类),逐页原因根本读不出来,但"有失败信息读不出来"这件事必须说。
    "malformed_failed_list": "沙箱回的失败清单整体格式不合法,逐页原因读不出来",
    #: Task 4b —— docx 的段落序号解析成页号时的四种**具名**失败。一种都不许
    #: 退化成"拿 unit 当页号渲一张";每一句都要让模型知道下一步能做什么。
    #: 锚点原文由 ``_describe_failed_units`` 从记录的 ``anchor`` 字段另外带出,
    #: 不写进这里的固定句子。
    "anchor_empty": (
        "这处图前面没有正文可以当定位锚点(它在文档最开头),定不出它在第几页;"
        "换成同一份文档的 PDF 版再试,那种格式的编号本来就是页号"
    ),
    "anchor_not_found": (
        "转出来的 PDF 里找不到这处图上面那段文字(排版把它拆行或改写了),"
        "定不出它在第几页;换成同一份文档的 PDF 版再试"
    ),
    "anchor_ambiguous": (
        "这处图上面那段文字在 PDF 里不止出现在一页上,定不到唯一一页,"
        "不拿第一页硬凑;换成同一份文档的 PDF 版再试"
    ),
    "unit_not_in_inventory": (
        "这份文档的图清单里没有这个编号;回 read_document 的清单里核对「第 N 处」那个数字再调一次"
    ),
}

#: 渲成功、但页号是**回落**得来的,那句必须说出来的话(Task 4b)。
#: ``rendered`` 里带 ``note`` 的条目走这张表。
_RENDER_NOTE_REASONS: Final[dict[str, str]] = {
    "anchor_only_fallback": (
        "这一页是**按上文的文字锚点**定位的,不是按图本身的位置钉死的 —— 转出来的"
        "PDF 在锚点那一页和紧邻的下一页都没有可识别的位图(图表、SmartArt、"
        "EMF/WMF 矢量图转出来就是这种情况)。图有可能落在紧邻的下一页上。"
    ),
}

#: New-M1 那条记账用的 unit 占位:产出记录本身就不是 Mapping 时,页号无从读起。
_UNKNOWN_UNIT: Final = "?"


def _is_plain_int(value: object) -> TypeGuard[int]:
    """真·整数(``True``/``False`` 不算)—— 播报页号前的那道闸。

    ``bool`` 是 ``int`` 的子类,``True`` 会被渲成"第 True 页"。
    """
    return isinstance(value, int) and not isinstance(value, bool)


#: ``unit`` 在对模型的文案里用哪个量词。pptx/pdf 的 unit 就是页号(「第 3 页」);
#: docx 的 unit 是图清单里的条目编号,与 ``document_figures._describe_figure``
#: 渲的「第 3 处」必须是同一个词 —— 模型是照着那份清单来调这个工具的。
_UNIT_LABELS: Final[dict[str, str]] = {"docx": "处"}
_DEFAULT_UNIT_LABEL: Final = "页"
#: 锚点原文在失败文案里的截断长度,与 ``_describe_figure`` 的 40 同一个量级。
_ANCHOR_ECHO_CHARS: Final = 40


# 沙箱内渲染片段。``os`` / ``json`` / ``_P`` / ``_resolve`` 来自共享的
# ``_PRELUDE``(见 file_ops);``_docx_inventory`` 来自与 ``document_figures``
# 共用的 :data:`~orchestrator.tools.document_figures._DOCX_INVENTORY_FRAGMENT`
# (Task 4b —— 两处各抄一份会让 ``unit`` 指向不同的段落,而且不报错)。
_RENDER_MAIN = (
    """

import glob as _glob
import shutil
import subprocess
"""
    + _DOCX_INVENTORY_FRAGMENT
    + """

_FORM_FEED = chr(12)


def _norm_ws(text):
    return " ".join(text.split())


def _pdf_page_texts(pdf):
    # 每一页的正文, 空白折叠成单个空格; 读不出来返回 None(**不是**空列表 ——
    # 空列表会被下游当成"这份 PDF 一页都没有", 那是猜)。
    #
    # pdftotext 一次跑整篇, 页与页之间是换页符(0x0c); 最后一页后面**也有**
    # 一个(2026-09-23 实测 poppler 21.11.0), 所以 split 出来的最后一个元素恒为
    # ""。丢掉它是为了让这个列表"一项一页"这件事成立 —— 今天的 _resolve_docx_page
    # 只按 enumerate 取页号、不看长度, 所以留着那个空串也不会让页号错位; 这一行
    # 是给将来会看 len() 的人留的, 不是在修一个现存的 bug。**只丢这一个**: 真正
    # 的空白页本身也是 "", 多丢一个才会让它之后所有页号整体偏移一页。
    try:
        result = subprocess.run(["pdftotext", pdf, "-"], capture_output=True,
                                timeout=_P["convert_timeout_s"], check=False)
    except Exception:
        return None
    if result.returncode != 0:
        return None
    parts = result.stdout.decode("utf-8", "replace").split(_FORM_FEED)
    if parts and parts[-1] == "":
        parts.pop()
    return [_norm_ws(part) for part in parts]


def _row_short_edge_upper_pt(cols):
    # `pdfimages -list` 的一行 -> 这张位图在页面上**最大可能**的显示短边(pt);
    # 读不出来返回 None(调用方据此**保留**这一行, 见下面)。
    #
    # 列位置: page/num/type/width/height 从**左**数(0..4), x-ppi/y-ppi 从
    # **右**数(-4/-3, 右边还剩 size 与 ratio 两列)。ppi 不从左数是因为中间
    # 的 "object ID" 那两列对内联图像会塌成一个 "[inline]" token, 左数会整体
    # 错位; 右端这几列的宽度不受影响。
    #
    # ppi 在 -list 里是**取整**过的(实测 poppler 21.11.0), 反推尺寸因此带
    # 误差。这里算的是"按最小可能 ppi(ppi-0.5)反推出来的最大可能尺寸",
    # 让误差一律偏向**保留**这一行 —— 把一张真图误判成装饰图会把它从候选里
    # 删掉, 那才是会让页号选错的方向。
    try:
        w_px, h_px = float(cols[3]), float(cols[4])
        x_ppi, y_ppi = float(cols[-4]), float(cols[-3])
    except (ValueError, IndexError):
        return None
    if w_px <= 0 or h_px <= 0 or x_ppi <= 0.5 or y_ppi <= 0.5:
        return None
    return min(w_px * 72.0 / (x_ppi - 0.5), h_px * 72.0 / (y_ppi - 0.5))


def _pdf_figure_pages(pdf):
    # 转出来的 PDF 里**位图**落在哪些页(升序去重); 读不出来返回 None。
    #
    # 实测(poppler 21.11.0, 2026-09-23): 前两行是表头(列名 + 横线), 之后
    # 每**画一次**位图一行, 第 1 列是页号, 按页号升序。同一个 XObject 被多页
    # 引用(页眉 logo 的真实形状)时**每页各一行**; 带 alpha 的图会多出一行
    # type=smask、页号与它相同。矢量算子在这里一行都没有 —— LibreOffice 把
    # 图表/SmartArt/EMF 渲成的正是矢量算子, 那种图走回落分支。
    #
    # 装饰图按与清单侧**同一条**阈值(_MIN_EDGE_PT)滤掉: 页眉/页脚 logo 不在
    # word/document.xml 里(它住在 word/header1.xml), 清单根本看不见它, 而它
    # 在每一页都有行 —— 不滤的话"页号 >= 邻域页的第一行"恒等于邻域页自己,
    # 跨页那一档就永远修不对。**只丢能正面确认是装饰图的行**: 尺寸读不出来的
    # 行一律留着。
    try:
        result = subprocess.run(["pdfimages", "-list", pdf], capture_output=True,
                                timeout=_P["render_timeout_s"], check=False)
    except Exception:
        return None
    if result.returncode != 0:
        return None
    pages = set()
    for line in result.stdout.decode("utf-8", "replace").splitlines():
        cols = line.split()
        if not cols or not cols[0].isdigit():
            continue
        short_edge = _row_short_edge_upper_pt(cols)
        if short_edge is not None and short_edge < _MIN_EDGE_PT:
            continue
        pages.add(int(cols[0]))
    return sorted(pages)


def _docx_context(docx_full, pdf):
    # docx 的 unit 是**段落序号**, 渲染前必须解析成页号。返回 (ctx, 错误名)。
    try:
        figures, _skipped = _docx_inventory(docx_full)
    except Exception:
        return None, "docx_inventory_failed"
    # 同一段里有两张图时 _docx_inventory 给出两条 unit 相同的记录, 它们的
    # anchor 逐字相同, 收进 dict 自然合一(这层歧义在清单那边就存在: 两条
    # 都显示成"第 N 处")。
    anchors = {}
    for fig in figures:
        anchors[fig["unit"]] = fig.get("anchor") or ""
    pages = _pdf_page_texts(pdf)
    if pages is None:
        return None, "pdftotext_failed"
    image_pages = _pdf_figure_pages(pdf)
    if image_pages is None:
        return None, "pdfimages_failed"
    return {"anchors": anchors, "pages": pages, "image_pages": image_pages}, None


def _resolve_docx_page(ctx, unit):
    # 返回 (页号, 说明, 锚点文字)。页号为 None 表示**具名失败**——一处都不猜。
    anchors = ctx["anchors"]
    if unit not in anchors:
        return None, "unit_not_in_inventory", None
    anchor = _norm_ws(anchors[unit])
    if not anchor:
        return None, "anchor_empty", None
    hits = [n for n, text in enumerate(ctx["pages"], 1) if anchor in text]
    if not hits:
        return None, "anchor_not_found", anchor
    if len(hits) > 1:
        return None, "anchor_ambiguous", anchor
    near = hits[0]
    # anchor 是图**正上方**那段文字的尾部, 所以图要么在含 anchor 的那一页,
    # 要么被挤到紧邻的下一页 —— 邻域**之外**的位图行不是这处图, 不拿它定页。
    for page in ctx["image_pages"]:
        if page < near:
            continue
        if page <= near + 1:
            return page, None, anchor
        break
    return near, "anchor_only_fallback", anchor


def _render_sha(full):
    # 产出路径必须跟着**渲染输入**走, 不是只跟着路径走(回修第 4 轮 New-I1)。
    # 宿主侧的 out_rel 只按 (run_id, 路径) 算, 同一条路径换了内容拿到的是逐字
    # 相同的产出 rel —— 前三层的洞(pdftoppm 层、soffice 层、缓存层)共享的正
    # 是这一个前提。这个哈希在片段里算, 不在宿主侧算: ReadPageTool 手里只有
    # SandboxRuntime 这一条通道(没有 workspace store), 够不着这个文件。
    #
    # **dpi 也要进来**(回修第 5 轮 M-1): 变了会让产出字节变、而 rel 不变的量,
    # 一个都不能留在路径外面。dpi 是 build_render_wrapper 的公开参数(带默认值),
    # 今天生产路径写死 RENDER_DPI 所以不可达 —— 但"公开入口的前置条件没人执行
    # 就等于没有"正是审计 B 刚关掉的那个形状, 靠 docstring 钉不住。同一份文件
    # 按 100dpi 与 300dpi 渲出的字节不同, 拼进来之后 rel 也就不同了。
    # 其余参数核过: ws/rel 已在 doc_sha 里, units 已在 _u<unit>/page-NN 里,
    # 两个 timeout 只决定成功与否、不决定成功时的字节。
    #
    # 流式读: 上传里见过 47.8MB 的 deck, 不能一次读进内存。
    digest = hashlib.sha256()
    digest.update(("dpi=" + str(_P["dpi"]) + ";").encode())
    with open(full, "rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()[:_P["sha_hex_len"]]


def _fresh_dir(path):
    # 清空 + 重建, 并确认真的清干净了(回修第 3 轮 Minor-3)—— shutil.rmtree
    # 配 ignore_errors=True 会静默吞掉"path 是指向别处的目录符号链接"这类
    # 错误(rmtree 拒绝 follow 符号链接), 而 os.makedirs(exist_ok=True) 对一个
    # 符号链接指向的已存在目录不会报错, 于是"这里已经清空"这个前提会悄悄落
    # 空, 后面的 subprocess/glob 全部建在这个前提之上。返回 False 时调用方
    # 必须把它当一次真实失败播报, 不能当没看见继续往下走(否则又是 New-2/
    # Important-1 那个坑的第三次重演)。
    shutil.rmtree(path, ignore_errors=True)
    os.makedirs(path, exist_ok=True)
    return not (os.path.islink(path) or os.listdir(path))


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
    ext = os.path.splitext(full)[1].lower()
    # docx 多用两件 poppler 工具把段落序号解析成页号(见 _docx_context)。缺了
    # 它们只能猜页, 而猜页正是这个功能要消灭的东西 —— 与缺 soffice 同档处理。
    needed = ["soffice", "pdftoppm"]
    if ext == ".docx":
        needed += ["pdftotext", "pdfimages"]
    missing = [name for name in needed if shutil.which(name) is None]
    if missing:
        return {"ok": False, "error": "soffice_missing", "detail": ",".join(missing)}
    try:
        render_sha = _render_sha(full)
    except OSError as exc:
        return {"ok": False, "error": "io_error", "detail": str(exc)}
    # out_rel 是宿主给的**基**路径(按 run_id + 文档路径算), 渲染输入的哈希在它
    # 下面再开一层 —— 输入一变, 这一层就变, 整条产出 rel 跟着变(回修第 4 轮
    # New-I1)。宿主不需要预知这一层: 片段回的 rel 本来就要流回宿主、过
    # _validate_rendered_rel 那道闸。
    out_dir = os.path.join(_P["ws"], _P["out_rel"], render_sha)
    os.makedirs(out_dir, exist_ok=True)
    if ext == ".pdf":
        pdf = full
    else:
        # 每次转换自己的 outdir —— soffice 的输出名按 basename 派生, 同目录里
        # x.docx 与 x.pptx 会互相静默覆盖(实测撞到过)。渲染前先清空再建、并
        # 确认清空真的生效(回修第 3 轮 Important-1 —— 与下面 unit_dir 同一
        # 个坑, New-2 那一轮只堵了 pdftoppm 那一层, soffice/conv 这一层原样
        # 开着:out_dir 跨调用存活, 不清空的话上一轮的旧 pdf 还在, 这一轮
        # soffice 真失败(非零退出、不产 pdf)时 glob 会把旧 pdf 当成这一轮的
        # 转换结果, 把转换失败报成了成功, 返回旧文档的页)。
        conv = os.path.join(out_dir, "_pdf")
        if not _fresh_dir(conv):
            return {"ok": False, "error": "convert_failed", "detail": "conv_dir_not_clean"}
        try:
            result = subprocess.run(
                ["soffice", "--headless", "--norestore", "--convert-to", "pdf",
                 "--outdir", conv, full],
                capture_output=True, timeout=_P["convert_timeout_s"], check=False)
        except Exception as exc:
            return {"ok": False, "error": "convert_failed", "detail": type(exc).__name__}
        # 非零退出码本身就是失败信号(回修第 3 轮 Important-1)——不能只靠
        # "有没有产出 pdf"去反推 soffice 是不是真的成功了。
        if result.returncode != 0:
            return {
                "ok": False,
                "error": "convert_failed",
                "detail": "exit=" + str(result.returncode),
            }
        pdfs = _glob.glob(os.path.join(conv, "*.pdf"))
        if not pdfs:
            return {"ok": False, "error": "convert_failed"}
        pdf = pdfs[0]
    docx_ctx = None
    if ext == ".docx":
        docx_ctx, ctx_error = _docx_context(full, pdf)
        if ctx_error is not None:
            return {"ok": False, "error": ctx_error}
    rendered = []
    failed = []
    for unit in _P["units"]:
        # docx 的 unit 是段落序号不是页号, 先解析; 解析不出来就**具名失败**,
        # 绝不拿 unit 当页号硬渲 —— 那会渲出一张无关页并声称它是这处图。
        page, note, anchor = unit, None, None
        if docx_ctx is not None:
            page, note, anchor = _resolve_docx_page(docx_ctx, unit)
            if page is None:
                record = {"unit": unit, "why": note}
                if anchor:
                    record["anchor"] = anchor
                failed.append(record)
                continue
        # 每个 unit 自己的私有产出目录(回修 C2)—— out_dir 跨调用累积, 一个
        # 只按 unit 数字子串匹配的 glob 会把其它 unit 的残留产物错当成这一页。
        # 渲染前先清空再建、并确认清空真的生效(回修第 2 轮 New-2 + 第 3 轮
        # Minor-3)——私有目录本身只挡跨 unit 混淆, 挡不住"同一个 unit 被重
        # 渲染、这一次失败了"这种情况:不清空的话旧文件还在, 这次 pdftoppm
        # 就算失败也会被 glob 到上一次的旧文件, 把渲染失败报成了成功(实测
        # 撞到)。清空之后, 私有目录里才真的只可能有这个 unit 这一次刚产出
        # 的那一个文件, glob 不需要也不许带 unit 后缀约束。
        unit_dir = os.path.join(out_dir, _P["unit_prefix"] + str(unit))
        if not _fresh_dir(unit_dir):
            failed.append({"unit": unit, "why": "unit_dir_not_clean"})
            continue
        prefix = os.path.join(unit_dir, _P["page_stem"])
        try:
            result = subprocess.run(
                ["pdftoppm", "-jpeg", "-r", str(_P["dpi"]),
                 "-f", str(page), "-l", str(page), pdf, prefix],
                capture_output=True, timeout=_P["render_timeout_s"], check=False)
        except Exception as exc:
            failed.append({"unit": unit, "why": type(exc).__name__})
            continue
        # 非零退出码本身就是失败信号(回修第 2 轮 New-2)——接住返回值, 不能
        # 只靠"有没有产出文件"去反推 pdftoppm 是不是真的成功了。
        if result.returncode != 0:
            failed.append({"unit": unit, "why": "pdftoppm_failed"})
            continue
        # pdftoppm 把页号补零到总页数的宽度(22 页 -> page-01.jpg), 所以必须
        # glob, 不许拼文件名。
        hits = sorted(_glob.glob(prefix + "-*.jpg"))
        if not hits:
            failed.append({"unit": unit, "why": "not_rendered"})
            continue
        got = hits[-1]
        # "page" 是**真正渲了哪一页**, 与 unit 不是一回事(docx 的 unit 是图
        # 清单编号)。宿主拿它播报, 不许拿 unit 冒充页号 —— 一个具体而错误的
        # 页码比不报页码坏得多(回修第 4 轮 New-M2 的同一条教训)。
        item = {"unit": unit, "page": page,
                "rel": os.path.relpath(got, _P["ws"]),
                "bytes": os.path.getsize(got)}
        if note:
            item["note"] = note
        rendered.append(item)
    if not rendered:
        return {"ok": False, "error": "render_failed", "failed": failed}
    return {"ok": True, "rendered": rendered, "failed": failed}


print(json.dumps(_main()))
"""
)


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
    """沙箱片段:把 ``ws/rel`` 的 ``units`` 页渲成 jpeg。

    ``out_rel`` 是**基**路径,不是最终产出目录 —— 片段会在它下面按**渲染输入**
    (文档字节 + ``dpi``)的哈希再开一层(见模块 docstring 第 6 条),每个 unit
    再开一层自己的私有目录,最终落在
    ``ws/out_rel/<render-sha>/_u<unit>/page-NN.jpg``。

    ``units`` 在这里**保序去重**(回修第 4 轮 审计 B)。片段的前置条件是
    "``units`` 不重复"——重复时第 2 圈会 ``rmtree`` 掉第 1 圈刚记进 ``rendered``
    的产物,回出一个 ``ok: true`` 却同时把同一个 unit 记进 ``rendered`` 和
    ``failed``、且 ``rendered`` 里那条 rel 指向已被删除文件的自相矛盾信封。
    生产路径上 :func:`_require_units` 已经去过重,但它是**调用方**的去重,不是
    这个公开入口自己的;这个函数是模块级公开函数,测试与未来的第二个调用方都
    直接够得着它,前置条件没人执行就等于没有。这不是第二份真源,是公开入口执行
    它自己 docstring 里写死的前置条件 —— :func:`_require_units` 保留去重另有
    理由(页数预算 + "第 X、Y 页"那句话要报对),两处职责不同。
    """
    return _snippet(
        {
            "ws": ws,
            "rel": rel,
            "units": list(dict.fromkeys(units)),
            "out_rel": out_rel,
            "dpi": dpi,
            # 落点形状的这三段由片段拼, 但值从共享真源来(回修第 5 轮 I-1):
            # expert_work.persistence 的 RENDERED_FIGURE_* 一组常量, 同时也是
            # is_cacheable_image_ref 判"这是不是一张渲染页"用的那一组。片段是
            # 一段不能 import 的字符串, 所以当参数喂进去, 而不是在里面再写一
            # 份字面量。
            "sha_hex_len": RENDERED_FIGURE_SHA_HEX_LEN,
            "unit_prefix": RENDERED_FIGURE_UNIT_PREFIX,
            "page_stem": RENDERED_FIGURE_PAGE_STEM,
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
    # 保序去重(回修第 3 轮 Important-2 起)。**防死 ref 这件事已经不在这一层**
    # (回修第 5 轮 M-4 —— 原注释把它写成这里去重的主要理由,与
    # build_render_wrapper 的新 docstring 打架):重复 unit 造成的
    # "rmtree 删掉上一圈刚记进 rendered 的产物 → 死 ref" 现在由公开入口
    # build_render_wrapper 自己执行前置条件挡掉(回修第 4 轮 审计 B),那才是
    # 无论谁来调都成立的那一道。
    #
    # 这里保留去重只为**一件**事:页数预算 —— [3, 3] 不该吃掉 MAX_PAGES_PER_CALL
    # 里的两页。M-4 换上来的第二条理由("页号播报要报对")经实测是假的:把这一层
    # 的去重拿掉,build_render_wrapper 自己那一道仍然只把 [3] 喂给片段,播报照样
    # 只报一次。**写一个假理由和写一个错理由是同一件事**,所以它被删掉而不是保留。
    return list(dict.fromkeys(units))


def _reject_unrenderable_format(ext: str) -> str | None:
    """``ext``(不带点、小写)能不能渲染;不能就返回拒绝文案,能就 ``None``。"""
    if ext in RENDERABLE_EXTENSIONS:
        return None
    reason = _UNSUPPORTED_FORMAT_REASONS.get(ext)
    if reason is not None:
        return reason
    label = f".{ext}" if ext else "(无扩展名)"
    renderable = " / ".join(sorted(RENDERABLE_EXTENSIONS))
    return f"read_page 不支持 {label} 这种格式 —— 目前只能渲染 {renderable}。"


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
    "不吞" 是对 ref 说的,不是对模型说的:``ReadPageTool.call``(回修第 2 轮
    New-3)还要把被剔除的 unit 并进 ``failed`` 播报里告诉模型"这一页丢了",
    单单在这里返回 ``None`` 而调用方不接话,模型看到的就是"请求了 3 页,只字
    不提第 2 页"的沉默失败。
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

    **这个值只认路径,不认内容** —— 它是产出目录的**基**路径的一段,不是文档
    身份的全部。同一条路径换了内容,这个值一个字都不变;把内容那一维加进产出
    路径是片段里 ``_render_sha`` 的活(回修第 4 轮 New-I1,见模块 docstring
    第 6 条)—— 那个哈希只能在沙箱里算,``ReadPageTool`` 手里没有 workspace
    store,够不着这个文件。
    """
    return hashlib.sha256(f"{ws}/{rel}".encode()).hexdigest()[:RENDERED_FIGURE_SHA_HEX_LEN]


def _explain_error(kind: str, detail: object) -> str:
    explanation = _ERROR_EXPLANATIONS.get(kind, _UNKNOWN_ERROR_EXPLANATION)
    return f"{explanation}(detail: {detail})" if detail else explanation


def _normalize_failed(raw: object) -> list[Any]:
    """把片段回来的 ``failed`` 归一成一个列表,**不静默吃掉形状不对的输入**。

    回修第 5 轮 I-2 —— ``call()`` 原来是
    ``list(raw) if isinstance(raw, list) else []``:沙箱回一个 dict(或别的什么)
    当 ``failed`` 时,整块失败信息就这么没了,模型一个字都读不到。与 New-M1
    (``rendered`` 侧)是同一条通路的另一半、同样的可达性。

    ``None``(键缺席,正常情况)→ 空列表。其它非列表 → 一条"清单整体不合法"的
    记录,让它走和逐页失败**同一条**播报路径,不另起一套文案。
    """
    if raw is None:
        return []
    if isinstance(raw, list):
        return list(raw)
    return [{"unit": _UNKNOWN_UNIT, "why": "malformed_failed_list"}]


def _describe_failed_units(failed: object, *, unit_label: str = _DEFAULT_UNIT_LABEL) -> str:
    """把 ``failed: [{"unit": n, "why": ...}]`` 渲成一句人话(回修 I4)。

    ``unit_label`` 是 ``unit`` 的量词(见 :data:`_UNIT_LABELS`)—— docx 的 unit
    不是页号,照着"第 N 页"说会把一个**不存在的页码**塞给模型。

    ``anchor`` 字段(Task 4b,只有 docx 的锚点类失败带)会原样回显给模型:
    失败时模型最需要知道的就是"我问的这处图,平台是拿哪段文字去定位的",
    它在 ``read_document`` 的正文里能找到那一段,据此换个编号或换份文档。

    空输入 → 空串(不加多余的句子)。

    回修第 5 轮 I-2 —— 不是 Mapping 的元素**也要说话**。这里原来是一句裸
    ``continue``:沙箱回 ``["bogus", None, 7, {"unit": 3, ...}]`` 时模型只读到
    第 3 页那一条,另外三条一个字不提。这是本任务第三次出现"修在证据指的那一行、
    而不是这一类通路"——New-M1 修的是 ``rendered`` 侧的同一个裸 ``continue``,
    这一侧当时没跟着改。
    """
    if not isinstance(failed, list) or not failed:
        return ""
    parts: list[str] = []
    for item in failed:
        anchor: object = None
        if isinstance(item, Mapping):
            unit = item.get("unit")
            why = str(item.get("why", "unknown"))
            anchor = item.get("anchor")
        else:
            unit, why = _UNKNOWN_UNIT, "malformed_failed_item"
        reason = _FAILED_UNIT_REASONS.get(why, why)
        echo = f";平台是拿这段文字定位的:{str(anchor)[:_ANCHOR_ECHO_CHARS]}" if anchor else ""
        parts.append(f"第 {unit} {unit_label}没取到({reason}{echo})")
    return "".join(f"{part}。" for part in parts)


def _describe_render_notes(rendered: Sequence[Mapping[str, Any]], *, unit_label: str) -> str:
    """成功渲出来、但页号是**回落**得来的那些条目,把保留意见说给模型(Task 4b)。

    这是"渲成功"与"页号可信"两件事的分离:回落分支给出的页是锚点那一页,
    它**不是**被图本身的位置钉死的。不说出来,模型会把一张可能不含目标图的页
    当成确凿证据 —— 那正是这个功能要消灭的静默失效换个位置重现。
    """
    parts: list[str] = []
    for item in rendered:
        note = item.get("note")
        if not note:
            continue
        explanation = _RENDER_NOTE_REASONS.get(str(note), str(note))
        parts.append(f"第 {item.get('unit')} {unit_label}:{explanation}")
    return "".join(parts)


@dataclass
class ReadPageTool:
    """按需把文档单页渲成 jpeg(暴露为 ``read_page``)。"""

    client: SandboxRuntime
    #: skill-runtime §5.1 — activated skill files seeded under /opt/skills/<agent_key>/.
    skill_seed_files: tuple[tuple[str, bytes], ...] = ()

    @property
    def spec(self) -> ToolSpec:
        # 回修第 2 轮 New-1 —— "哪些格式能渲染"这句不能手写字面量:15 行外的
        # _reject_unrenderable_format 已经从 RENDERABLE_EXTENSIONS 派生了,这里
        # 手写一份会在 Task 4b 给 RENDERABLE_EXTENSIONS 加 docx 之后立刻漂移——
        # 模型每一轮读到的描述仍然说 docx 会被拒,子集钉与身份钉都管不到 prose,
        # 没有任何测试会红。改成从同一个集合插值,并且不点名"谁被拒"(不然又是
        # 同一个坑换个位置)。
        renderable = " / ".join(sorted(ext.upper() for ext in RENDERABLE_EXTENSIONS))
        return ToolSpec(
            name="read_page",
            description=(
                "Render a specific page/slide of a document in your own workspace "
                "into an image, so you can actually look at a figure or chart the "
                "document's figure map (shown by read_document) told you about. "
                f"Only {renderable} are renderable today; other formats are refused "
                "with an explanation. "
                "'units' are the 1-based entry numbers the figure map shows "
                "(page/slide numbers for PDF and PPTX; for DOCX they identify "
                "the figure, and the page is resolved for you); at most "
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
                            "1-based entry numbers from the figure map to render "
                            f"(max {MAX_PAGES_PER_CALL})."
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
        out_rel = f"{WORKSPACE_OVERFLOW_DIR}/{ctx.run_id}/{RENDERED_FIGURE_DIR}/{doc_sha}"
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
        label = _UNIT_LABELS.get(ext, _DEFAULT_UNIT_LABEL)
        if not env.get("ok"):
            kind = str(env.get("error", "unknown"))
            msg = f"无法渲染 {raw}:{_explain_error(kind, env.get('detail'))}"
            per_unit = _describe_failed_units(
                _normalize_failed(env.get("failed")), unit_label=label
            )
            if per_unit:
                msg = f"{msg} {per_unit}"
            return ToolResult(content=msg)

        # 回修 C1 —— 沙箱回来的 rel 是相对沙箱自己的 exec 视图算的;翻成用户
        # 根相对路径才能造出 NasWorkspaceImageResolver 能解析的 ref。
        scope = store_scope(ws, agent_key=ctx.agent_key)
        refs: list[str] = []
        #: ``(unit, page)`` —— 两者对 pptx/pdf 相等,对 docx 不等(Task 4b)。
        rendered_units: list[tuple[int, int]] = []
        #: 页号是回落得来的那些条目,原样留给 ``_describe_render_notes``。
        notes: list[Mapping[str, Any]] = []
        # 回修第 2 轮 New-3 —— _validate_rendered_rel 剔掉的 unit 不能悄悄消失:
        # 函数自己的 docstring 说"不吞",但只做到了不接受那个 ref,没做到告诉
        # 模型"你请求的这一页丢了"。并进下面统一的 failed 播报里,不单独起一套
        # 文案。
        rejected: list[Mapping[str, Any]] = []
        for item in env.get("rendered") or ():
            if not isinstance(item, Mapping):
                # 回修第 4 轮 New-M1 —— 不是 Mapping 的产出记录也要记账,不能
                # 裸 continue 丢掉:请求 3 页、沙箱回 1 张图 + 两个 "bogus" 时,
                # 模型读到的是"已渲染 …… 第 1 页",另外两页一个字不提,与
                # New-3/Minor-2 堵的那条静默失效逐字同形。
                rejected.append({"unit": _UNKNOWN_UNIT, "why": "malformed_item"})
                continue
            unit = item.get("unit")
            safe_rel = _validate_rendered_rel(item.get("rel"))
            if safe_rel is None:
                # 回修第 3 轮 Minor-2 —— 无条件记账,不只挑 unit 是 int 的那
                # 一支:沙箱回一个字符串/None 形态的 unit 时,New-3 想堵的沉默
                # 失败原样保留。_describe_failed_units 只是把 unit 塞进
                # f-string,任何类型都能渲成一句话。
                rejected.append({"unit": unit, "why": "rejected_rel"})
                continue
            host_rel = scoped_path(scope, safe_rel)
            refs.append(workspace_figure_ref(ctx.tenant_id, ctx.user_id, host_rel))
            # ``page`` 缺席时按 ``page == unit`` 处理 —— 那是 pptx/pdf 的**定义**
            # (unit 就是页号),片段对 docx 一律显式带上。这不是在替片段兜底:
            # unit 本身读不出来时下面照样退回只报张数。
            page = item.get("page", unit)
            if _is_plain_int(unit) and _is_plain_int(page):
                rendered_units.append((unit, page))
            if item.get("note"):
                notes.append(item)
        all_failed = _normalize_failed(env.get("failed")) + rejected
        if not refs:
            msg = f"无法渲染 {raw}:没有取到任何页。"
            per_unit = _describe_failed_units(all_failed, unit_label=label)
            if per_unit:
                msg = f"{msg} {per_unit}"
            return ToolResult(content=msg)

        # 回修第 4 轮 New-M2 —— 兜底值不能写成页码。原来是
        # `str(len(refs))` 直接插进"第 {pages} 页"这句**页码**文案:沙箱回
        # unit:"7" 与 unit:"8"(字符串形态)加两条合法 rel 时,模型被告知
        # "已渲染 d.pptx 第 2 页",而上下文里躺的是第 7、8 页 —— 一个具体而
        # 错误的页码比不报页码坏得多。读不出页号时就只报张数。
        #
        # Task 4b —— docx 的 unit 不是页号,"第 7 处"渲出来的可能是第 3 页。
        # 单说 unit 会让模型以为文档有第 7 页,单说页号又对不上它问的那处图,
        # 所以两个都说。pptx/pdf 的 unit 与 page 按定义相等,措辞一字不变。
        if rendered_units:
            if label != _DEFAULT_UNIT_LABEL:
                shown = "、".join(f"第 {u} {label}(文档第 {p} 页)" for u, p in rendered_units)
                head = f"已渲染 {raw} {shown}"
            else:
                pages = "、".join(str(u) for u, _ in rendered_units)
                head = f"已渲染 {raw} 第 {pages} {label}"
        else:
            head = f"已渲染 {raw} {len(refs)} 页"
        content = f"{head},已放进你的上下文 —— 需要仔细看细节时用 ask_image 问它。"
        caveats = _describe_render_notes(notes, unit_label=label)
        if caveats:
            content = f"{content} {caveats}"
        per_unit = _describe_failed_units(all_failed, unit_label=label)
        if per_unit:
            content = f"{content} {per_unit}"
        return ToolResult(
            content=content,
            meta={"pixel_budget_per_run": MAX_RENDER_PIXELS},
            state_updates={"viewed_figures": refs},
        )
