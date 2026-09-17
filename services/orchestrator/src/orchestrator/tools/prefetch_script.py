"""B-61 §4.3 —— 在**沙箱里**运行的预拉脚本。

平台把本模块的源码文本送进沙箱执行:``python - <inputs.json 绝对路径>``。下载因此
发生在沙箱的出网通道上,自动继承该 agent 的 ``sandbox.network`` 策略(spec §4.3 的
治理理由:预拉是替模型做它本来要做的事,就该受同样的约束)。

**自包含**:只用 stdlib,不 import 仓库任何东西 —— 沙箱里没有这个仓库。判定逻辑因此
可以被本地单测直接 import 覆盖(``test_prefetch_script.py``)。

**永不让 run 失败**:任何一个 URL 的任何一种失败都只是把它的 ``local_path`` 写成
``null``,脚本自己始终以 0 退出。

**内容寻址的共享缓存**(T11):文件落在 ``inputs/cache/<sha256(url)[:32]><ext>``,
与 ``inputs/<run_id>/`` **平级**,按 agent 共享。同一个 URL 跨轮只下一次
(``CACHE_TTL_S`` 内),既省带宽也止住工作区配额的无限增长 —— 两者本是同一个病的
两面(按 run 复制既是浪费也是增长曲线的分子)。

**按变量名的符号链接**(B-67 §4.1):每拉到(或命中)一个 site,就在 run 目录里建
``<链接名> -> ../cache/<digest><ext>``,``local_path`` 指链接。名字由变量名 + 路径 +
URL 后缀决定,与 control-plane 渲染层说的名字逐字相同。链接失效 = 已接受的降级。
"""

from __future__ import annotations

import contextlib
import hashlib
import http.client
import json
import os
import posixpath
import re
import sys
import tempfile
import time
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlparse

MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_TOTAL_BYTES = 128 * 1024 * 1024
TIMEOUT_S = 30

#: B-67 —— 下面这组常量与函数与宿主侧 ``orchestrator.tools.inputs_doc`` **逐字同义**
#: (常量 ``PARSED_KEY`` / ``SLUG_MAX_CHARS`` / ``MAX_PARSE_DEPTH`` / ``_SLUG_DROP`` /
#: ``_EXT_RE`` / ``_RESERVED_NAMES``;函数 ``_url_suffix`` / ``_slugify`` /
#: ``_site_description`` / ``_link_stem`` / ``_link_names`` / ``_root_key`` / ``_too_deep``)。
#: 这里不能 import 那边,只能复制;``test_copied_helpers_are_the_host_algorithm``(语法树逐个
#: 比对)与 ``test_link_names_match_the_host_side_implementation``(行为语料)钉住两边同义
#: —— 提示词里说的名字必须就是这里建出来的链接名。
PARSED_KEY = "value_parsed"
SLUG_MAX_CHARS = 40
MAX_PARSE_DEPTH = 32
_SLUG_DROP = re.compile(r"[^\w-]")
_EXT_RE = re.compile(r"^\.[A-Za-z0-9]{1,5}$")
#: 本脚本在 run 目录里自己写的两个文件:清单(宿主给的路径,文件名恒为 ``inputs.json``)
#: 与 :func:`_rewrite` 的临时文件。链接名绝不能占用它们。
_MANIFEST_NAME = "inputs.json"
_REWRITE_SUFFIX = ".tmp"
_RESERVED_NAMES = frozenset({_MANIFEST_NAME, _MANIFEST_NAME + _REWRITE_SUFFIX})


def _url_suffix(url: str) -> str:
    ext = posixpath.splitext(urlparse(url).path)[1]
    return ext if _EXT_RE.match(ext) else ""


def _slugify(text: str | None) -> str:
    if not text:
        return ""
    return _SLUG_DROP.sub("", text[:SLUG_MAX_CHARS])


def _site_description(root: Any, path: list[str | int]) -> str | None:
    cursor = root
    for step in path:
        if isinstance(step, int):
            item = cursor[step] if isinstance(cursor, list) and 0 <= step < len(cursor) else None
            desc = item.get("description") if isinstance(item, dict) else None
            return desc if isinstance(desc, str) else None
        if not isinstance(cursor, dict):
            return None
        cursor = cursor.get(step)
    return None


def _link_stem(var_name: str, path: list[str | int], description: str | None) -> str:
    keys: list[str] = []
    for step in path:
        if isinstance(step, int):
            head = ".".join([var_name, *keys])
            slug = _slugify(description)
            return f"{head}/{step}-{slug}" if slug else f"{head}/{step}"
        keys.append(_slugify(str(step)) or "_")
    return ".".join([var_name, *keys])


def _link_names(var_name: str, sites: list[tuple[list[str | int], str, str | None]]) -> list[str]:
    taken = set(_RESERVED_NAMES)
    out: list[str] = []
    for path, url, description in sites:
        stem, ext = _link_stem(var_name, path, description), _url_suffix(url)
        name = stem + ext
        n = 1
        while name in taken:
            n += 1
            name = f"{stem}-{n}{ext}"
        taken.add(name)
        out.append(name)
    return out


def _root_key(entry: dict[str, Any]) -> str:
    return PARSED_KEY if PARSED_KEY in entry else "value"


def _too_deep(value: Any) -> bool:
    stack = [(value, 0)]
    while stack:
        item, depth = stack.pop()
        if isinstance(item, dict):
            item = list(item.values())
        if not isinstance(item, list):
            continue
        if depth >= MAX_PARSE_DEPTH:
            return True
        stack.extend((child, depth + 1) for child in item)
    return False


def _linked_sites(var_name: str, value: Any) -> list[tuple[list[str | int], str, str]]:
    """一个变量(``value`` 是 ``_root_key`` 选中的那一份)的 site 与链接名 ——
    宿主 ``inputs_doc.linked_sites`` 在沙箱里的对应物。嵌套过深的值没有 site,与宿主同一道闸。
    """
    if _too_deep(value):
        return []
    found = _sites(value, [])
    names = _link_names(
        var_name, [(path, url, _site_description(value, path)) for path, url in found]
    )
    return [(path, url, name) for (path, url), name in zip(found, names, strict=True)]


def _inside_run_dir(run_dir: str, link_path: str) -> bool:
    """``link_path`` 所在目录解析后仍在 run 目录里吗?

    沙箱代码能把 ``run/m`` 换成指向别处的目录链接;跟着它删 / 建,就是在 run 目录之外
    删文件、放链接。只有解析后仍在 run 目录里,本脚本才动那个位置。
    """
    real_run = os.path.realpath(run_dir)
    real_parent = os.path.realpath(os.path.dirname(link_path))
    return os.path.commonpath([real_run, real_parent]) == real_run


def _link(run_dir: str, name: str, cache_dir: str, filename: str) -> str | None:
    """在 run 目录里建 ``name -> ../cache/<filename>`` 的**相对**符号链接;失败返回 ``None``。

    相对目标:NAS 视角与沙箱 ``/workspace`` 视角下都成立(exec view 是 agent 目录的 bind)。
    ``name`` 可能带子目录(``materials/0-x.mp4``),父目录顺手建。

    **只替换符号链接**:``os.symlink`` 到已存在路径会 ``FileExistsError``,所以同名的旧
    链接(续跑 / 同 run 再预拉)先删后建;同名的普通文件 / 目录不是本脚本建的,不删,返回
    ``None``。父目录解析后跑出 run 目录(沙箱代码种的目录链接)同样返回 ``None``。任何
    OSError 都只是「没建成」—— 调用方回落到 cache 路径,预拉不让 run 失败。
    """
    link_path = os.path.join(run_dir, name)
    target = os.path.relpath(os.path.join(cache_dir, filename), start=os.path.dirname(link_path))
    try:
        os.makedirs(os.path.dirname(link_path), exist_ok=True)
        if not _inside_run_dir(run_dir, link_path):
            return None
        if os.path.lexists(link_path):
            if not os.path.islink(link_path):
                return None
            os.unlink(link_path)
        os.symlink(target, link_path)
    except OSError:
        return None
    return name


def _unlink_stale(run_dir: str, name: str) -> None:
    """这次没拉到:删掉更早一次预拉在 ``name`` 上留下的链接,别让它与 null 的
    ``local_path`` 并存。只删 run 目录里的符号链接;失败不外抛(``local_path`` 照样要写 null)。
    """
    link_path = os.path.join(run_dir, name)
    with contextlib.suppress(OSError):
        if _inside_run_dir(run_dir, link_path) and os.path.islink(link_path):
            os.unlink(link_path)


#: 共享缓存目录名,挂在 ``inputs/`` 下、与 ``<run_id>/`` 平级。
CACHE_DIRNAME = "cache"

#: 缓存条目的新鲜期:窗口内命中就不重下,超期命中会重下并刷新 mtime。
#:
#: **命中时不 touch**(不调 ``os.utime``):于是 mtime 恒等于「最近一次下载时间」,
#: 也就是「最近引用时间」的 24 小时粒度近似 —— 回收闸按它过期只会打到真没人引用的
#: 条目。反过来加一次 touch,热条目就永远不会超期、也就永远不刷新内容,而 B-61 的
#: 起因恰恰是一个被换掉的 logo。
#:
#: **这是个带宽旋钮,不是保留期**:它只决定「同一个 URL 多久之内不重下」。条目在 NAS 上
#: 留多久由 control-plane 的 workspace janitor 独立决定(``_CACHE_TTL_S``,7 天),**不跟
#: 这个值走** —— 早先那版把两者绑成不等式,等于让改下载行为的人顺手改了存储保留期,已撤销。
CACHE_TTL_S = 24 * 3600

#: 按 content-type 决定「是不是素材」。前缀族 + 精确名两张表。
_TYPE_PREFIXES = ("image/", "video/", "audio/")
_TYPE_EXACT = frozenset(
    {
        "application/pdf",
        "application/msword",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-excel",
        "application/vnd.ms-powerpoint",
    }
)

_SUFFIX_BY_TYPE = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "audio/mpeg": ".mp3",
    "audio/wav": ".wav",
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
}


def _bare_type(content_type: str) -> str:
    return content_type.split(";")[0].strip().lower()


def content_type_ok(content_type: str) -> bool:
    """这个 content-type 算「素材」吗?``text/html`` 这类是地址不是素材,不存。"""
    bare = _bare_type(content_type)
    return bare.startswith(_TYPE_PREFIXES) or bare in _TYPE_EXACT


def pick_suffix(content_type: str, url: str) -> str:
    """扩展名:content-type 优先,回落到 URL 路径的扩展名,都没有就空串。"""
    bare = _bare_type(content_type)
    if bare in _SUFFIX_BY_TYPE:
        return _SUFFIX_BY_TYPE[bare]
    ext = posixpath.splitext(urlparse(url).path)[1]
    return ext if 1 < len(ext) <= 6 and ext.isascii() else ""


def cache_digest(url: str) -> str:
    """缓存条目的文件名主干:URL 的 sha256 取前 32 位 hex。

    只由 URL 决定:同一个地址跨轮、跨 run 落在同一个条目上(复用点),而租户给的
    字符串(变量名、结构里的键)一个字都不进文件名 —— 路径穿越这件事结构上就不
    存在了。用 sha256 是取它的抗碰撞性,不是当口令哈希用。
    """
    return hashlib.sha256(url.encode()).hexdigest()[:32]


def script_source() -> str:
    """本模块的源码文本 —— 平台下发进沙箱执行的就是它。"""
    with open(__file__, encoding="utf-8") as handle:
        return handle.read()


def _cached_name(cache_dir: str, digest: str) -> str | None:
    """找这个 digest 的新鲜缓存条目;没有(或只剩超期的)就 ``None``。

    **按 digest 前缀扫目录**,不是拿 URL 的扩展名去拼文件名再探:后缀由响应的
    content-type 定,``/logo`` 回 ``image/png`` 就落成 ``<digest>.png``,照 URL 猜
    探的是空后缀的 ``<digest>`` —— 永远探不到、每轮重下一份,整个缓存等于白做。

    **同一个 URL 可能留下多个兄弟条目**:后缀随响应变(同一个 ``/logo`` 今天回
    ``image/png``、明天回 ``image/jpeg``,或 CDN 偶尔回 ``application/octet-stream``
    而回落到 URL 扩展名),而 ``os.replace`` 只盖同名的那个。所以超期条目必须**跳过
    并顺手删掉**,不能一碰到就 ``None`` 返回:``os.scandir`` 是文件系统顺序(不是字典
    序),只要超期的那个先被扫到,旁边新鲜的就被永久遮住 —— 每轮重下、每轮再写一份,
    正好退回本任务要治的那个病,而且是不确定的、自己不会好的形态。

    同样的理由,找到新鲜条目也**不提前返回**:剩下的兄弟条目要扫完才删得干净,否则
    「删不删得掉」又取决于扫描顺序。

    目录不存在 / 读不了都只算「没命中」:这条路上的任何异常都不该冒出去(预拉永不
    让 run 失败),最坏结果是多下一次。
    """
    found: str | None = None
    try:
        with os.scandir(cache_dir) as entries:
            for entry in entries:
                if not entry.name.startswith(digest):
                    continue
                try:
                    fresh = time.time() - entry.stat().st_mtime < CACHE_TTL_S
                except OSError:
                    # 并发的另一个 run 正好把这个超期条目删掉了(就是下面这段干的)
                    # ——当它不存在,接着扫,别让一个消失的条目把已经找到的命中作废。
                    continue
                if fresh:
                    if found is None:
                        found = entry.name
                    continue
                with contextlib.suppress(OSError):
                    os.unlink(entry.path)
    except OSError:
        return None
    return found


def _store(cache_dir: str, name: str, body: bytes) -> None:
    """把内容落成 ``cache_dir/name``:同目录唯一临时文件 → chmod → ``os.replace``。

    * 唯一临时文件(``mkstemp``):两个并发 run 撞同一个 URL 时各写各的,不会互相
      踩到半截内容;
    * ``os.chmod(tmp, 0o644)``:``mkstemp`` 恒建 0600 而 ``os.replace`` 保权限位,
      不显式改就把条目静默降成 0600,跨 uid 的读方再次读不到(W2-BUG-1 的原病);
    * ``os.replace`` 而不是直接写目标:目标路径上要么是完整文件、要么还是上一版,
      不会出现半截 —— 半截条目下一轮会被当成命中直接喂给模型。

    **一条勘误**:这里原来写着「超期重下时这一步直接盖掉旧条目,不用先删」,那条路径
    今天基本不发生 —— 走到 ``_store`` 之前 ``_cached_name`` 已经把同 digest 的超期条目
    (含同名那个)``unlink`` 掉了,所以目标通常根本不存在。``os.replace`` 真正还在守的
    是**原子性**,以及并发的另一个 run 恰好刚写完同名条目时的覆盖语义。
    """
    os.makedirs(cache_dir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=cache_dir)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(body)
        os.chmod(tmp, 0o644)
        os.replace(tmp, os.path.join(cache_dir, name))
    except BaseException:
        # 失败不把临时文件留在 cache/ 里:这是个计工作区配额的共享目录,本任务
        # 治的就是它的无限增长。清理本身再失败也不能盖掉真正的原因。
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _fetch(url: str, cache_dir: str, budget: int) -> tuple[str | None, int]:
    """拉一个 URL 进共享缓存。返回 ``(缓存文件名 or None, 真正下载的字节数)``。

    命中缓存返回 ``(文件名, 0)`` —— 一个请求都不发,预算也不扣;任何失败都返回
    ``(None, 0)``。
    """
    digest = cache_digest(url)
    cached = _cached_name(cache_dir, digest)
    if cached is not None:
        return cached, 0
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "expert-work-prefetch/1"})  # noqa: S310
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:  # noqa: S310
            content_type = response.headers.get("Content-Type", "")
            if not content_type_ok(content_type):
                return None, 0
            declared = response.headers.get("Content-Length")
            declared_len = int(declared) if declared is not None and declared.isdigit() else None
            if declared_len is not None and declared_len > MAX_FILE_BYTES:
                return None, 0
            cap = min(MAX_FILE_BYTES, budget)
            body = response.read(cap + 1)
        if len(body) > cap:
            return None, 0
        # 服务端声明了长度却没发够(连接提前关闭):body 会被静默截断而不抛异常,
        # 不比对就会把半张图片当命中存下——比不上不存,同其它失败一样降级。
        if declared_len is not None and len(body) != declared_len:
            return None, 0
        name = digest + pick_suffix(content_type, url)
        _store(cache_dir, name, body)
        return name, len(body)
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError, TimeoutError):
        return None, 0


def _rewrite(inputs_path: str, doc: dict[str, Any]) -> None:
    """把当前文档原子地盖回 ``inputs.json``(同目录临时文件 + ``os.replace``)。

    每拉完一个 site 就调一次:整段 exec 有墙钟上限,超了会被 SIGKILL,而写在
    最后一步的「一次性改写」在那种情况下等于**一个 local_path 都没落下**——文件明明
    已经躺在 ``inputs/cache/`` 里,却没有一条记录指向它们,模型这一轮只能自己重下。
    逐个落盘后,被杀只损失还没拉完的那些。
    同目录 + ``os.replace`` 保证读的人要么看到上一版、要么看到新版,不会读到半份。
    """
    tmp_path = inputs_path + _REWRITE_SUFFIX
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(doc, handle, ensure_ascii=False)
    os.replace(tmp_path, inputs_path)


def main(argv: list[str]) -> int:
    inputs_path = argv[1]
    try:
        with open(inputs_path, encoding="utf-8") as handle:
            doc = json.load(handle)
    except (OSError, ValueError, RecursionError):
        # 读不到 / 解不了 inputs.json(比如续跑时文件已被沙箱代码弄坏;嵌套极深时
        # json.load 抛的是 RecursionError)——没有文档就没有东西可预拉,同样不让 run 失败。
        print(json.dumps({"prefetch": []}, ensure_ascii=False))
        return 0
    # 文档的形状不可信:它是上一次运行/沙箱代码碰过的文件,可能是一个 list、
    # variables 可能不是对象。JSON 解析成功 != 形状对,不判就是一个 AttributeError
    # 冒到 main 外面,预拉整轮丢掉。形状不对就当没有东西可拉,不改文件。
    variables = doc.get("variables") if isinstance(doc, dict) else None
    if not isinstance(variables, dict):
        print(json.dumps({"prefetch": []}, ensure_ascii=False))
        return 0
    run_dir = os.path.dirname(inputs_path)
    run_dirname = os.path.basename(run_dir)
    # cache/ 挂在 run 目录的**父目录**下(``inputs/cache/``),与 ``<run_id>/`` 平级:
    # 内容寻址的条目按 agent 共享、跨轮复用,放进 run 目录就退回「每轮重下一份」。
    cache_dir = os.path.join(os.path.dirname(run_dir), CACHE_DIRNAME)
    cache_prefix = posixpath.join("inputs", CACHE_DIRNAME)
    budget = MAX_TOTAL_BYTES
    report: list[dict[str, object]] = []

    for name, entry in variables.items():
        if not isinstance(entry, dict):
            continue
        root = _root_key(entry)
        # B-67 §4.1 —— 链接名在拉之前就定(与渲染层同一算法),拉到一个建一个。
        for path, url, link in _linked_sites(name, entry.get(root)):
            hit = False
            used = 0
            try:
                filename, used = _fetch(url, cache_dir, budget)
                hit = filename is not None
                rel = None
                if filename is None:
                    _unlink_stale(run_dir, link)
                else:
                    linked = _link(run_dir, link, cache_dir, filename)
                    # 链接建成 → local_path 指链接(可读的名字);建不成 → 指 cache(老形态)。
                    rel = (
                        posixpath.join("inputs", run_dirname, linked)
                        if linked is not None
                        else posixpath.join(cache_prefix, filename)
                    )
                _assign(entry, root, path, rel)
                _rewrite(inputs_path, doc)
            except Exception:
                # 一个 site 的任何意外(文档结构与 _sites 的判定不一致、落盘失败
                # ……)都不该带走其它 site 已经拿到的结果:记一条 miss,继续下一个。
                # 不打异常内容——里面可能带着 URL(spec §八)。
                hit = False
            budget -= used
            report.append({"variable": name, "hit": hit, "bytes": used})

    # 只打名字/命中/字节数,不打值也不打 URL(spec §八)。
    print(json.dumps({"prefetch": report}, ensure_ascii=False))
    return 0


def _sites(
    value: Any, prefix: list[str | int], assignable: bool = True
) -> list[tuple[list[str | int], str]]:
    """``assignable`` = 这一层的 URL 旁边有没有地方记 ``local_path``。

    只有变量整个 ``value``(记在变量对象上)和 dict 的某个键(记成同级的
    ``local_path``)两种位置记得下。列表里的裸字符串没有:``["https://a"]`` 的第 0
    项要挂 ``local_path`` 只能改写字符串自己——``_assign`` 走到那里会
    ``cursor["local_path"] = rel`` 打在一个 list 上直接 ``TypeError``,一条这样的
    URL 就能让整轮预拉的结果全丢。这类 URL 不算 site:平台不预拉,模型照旧自己下
    载。与宿主侧 ``inputs_doc._walk`` 是同一条规则,必须同义(见
    ``test_site_walk_matches_the_host_side_implementation``)。
    """
    if isinstance(value, str) and (value.startswith("http://") or value.startswith("https://")):
        return [(list(prefix), value)] if assignable else []
    if isinstance(value, dict):
        found: list[tuple[list[str | int], str]] = []
        for key, item in value.items():
            # value 是租户直接传的 JSON,调用方可能自己就塞了一个叫 local_path 的
            # 字段(值可以是任意字符串,包括 URL);不挡住它会被当成待预拉的 site,
            # 预拉后又被平台自己回填的 local_path 覆盖——等于把调用方指定的地址喂给
            # 沙箱的出网请求。这里挡的是租户输入,不是只挡本脚本自己回填的值,与宿主
            # 侧 inputs_doc._walk 的同一道闸保持同义(见
            # test_site_walk_matches_the_host_side_implementation)。
            if key != "local_path":
                found.extend(_sites(item, [*prefix, key], True))
        return found
    if isinstance(value, list):
        found = []
        for index, item in enumerate(value):
            found.extend(_sites(item, [*prefix, index], False))
        return found
    return []


def _assign(entry: dict[str, Any], root: str, path: list[str | int], rel: str | None) -> None:
    if not path:
        entry["local_path"] = rel
        return
    cursor = entry[root]
    for step in path[:-1]:
        cursor = cursor[step]
    cursor["local_path"] = rel


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
