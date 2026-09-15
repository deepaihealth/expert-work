"""B-61 §4.3 —— 在**沙箱里**运行的预拉脚本。

平台把本模块的源码文本送进沙箱执行:``python - <inputs.json 绝对路径>``。下载因此
发生在沙箱的出网通道上,自动继承该 agent 的 ``sandbox.network`` 策略(spec §4.3 的
治理理由:预拉是替模型做它本来要做的事,就该受同样的约束)。

**自包含**:只用 stdlib,不 import 仓库任何东西 —— 沙箱里没有这个仓库。判定逻辑因此
可以被本地单测直接 import 覆盖(``test_prefetch_script.py``)。

**永不让 run 失败**:任何一个 URL 的任何一种失败都只是把它的 ``local_path`` 写成
``null``,脚本自己始终以 0 退出。
"""

from __future__ import annotations

import http.client
import json
import os
import posixpath
import sys
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlparse

MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_TOTAL_BYTES = 128 * 1024 * 1024
TIMEOUT_S = 30

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


def target_name(var_name: str, path: list[str | int], suffix: str) -> str:
    """文件名 = 变量名 + 位置 + 扩展名。位置进名字,两个 URL 永不撞。"""
    parts = [str(var_name), *[str(p) for p in path]]
    for part in parts:
        if not part or "/" in part or part in {".", ".."}:
            msg = f"unsafe path component: {part!r}"
            raise ValueError(msg)
    return ".".join(parts) + suffix


def script_source() -> str:
    """本模块的源码文本 —— 平台下发进沙箱执行的就是它。"""
    with open(__file__, encoding="utf-8") as handle:
        return handle.read()


def _fetch(
    url: str, dest_dir: str, var_name: str, path: list[str | int], budget: int
) -> tuple[str | None, int]:
    """拉一个 URL。返回 ``(文件名 or None, 消耗字节数)``;任何失败都返回 ``(None, 0)``。"""
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
        name = target_name(var_name, path, pick_suffix(content_type, url))
        os.makedirs(dest_dir, exist_ok=True)
        with open(os.path.join(dest_dir, name), "wb") as handle:
            handle.write(body)
        return name, len(body)
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError, TimeoutError):
        return None, 0


def main(argv: list[str]) -> int:
    inputs_path = argv[1]
    try:
        with open(inputs_path, encoding="utf-8") as handle:
            doc = json.load(handle)
    except (OSError, ValueError):
        # 读不到 / 解不了 inputs.json(比如续跑时文件已被沙箱代码弄坏)——没有
        # 文档就没有东西可预拉,同样不让 run 失败。
        print(json.dumps({"prefetch": []}, ensure_ascii=False))
        return 0
    run_dir = os.path.dirname(inputs_path)
    files_dir = os.path.join(run_dir, "files")
    rel_prefix = posixpath.join("inputs", os.path.basename(run_dir), "files")
    budget = MAX_TOTAL_BYTES
    report: list[dict[str, object]] = []

    for name, entry in doc.get("variables", {}).items():
        for path, url in _sites(entry.get("value"), []):
            filename, used = _fetch(url, files_dir, name, path, budget)
            budget -= used
            rel = posixpath.join(rel_prefix, filename) if filename else None
            _assign(entry, path, rel)
            report.append({"variable": name, "hit": filename is not None, "bytes": used})

    with open(inputs_path, "w", encoding="utf-8") as handle:
        json.dump(doc, handle, ensure_ascii=False)
    # 只打名字/命中/字节数,不打值也不打 URL(spec §八)。
    print(json.dumps({"prefetch": report}, ensure_ascii=False))
    return 0


def _sites(value: Any, prefix: list[str | int]) -> list[tuple[list[str | int], str]]:
    if isinstance(value, str) and (value.startswith("http://") or value.startswith("https://")):
        return [(list(prefix), value)]
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
                found.extend(_sites(item, [*prefix, key]))
        return found
    if isinstance(value, list):
        found = []
        for index, item in enumerate(value):
            found.extend(_sites(item, [*prefix, index]))
        return found
    return []


def _assign(entry: dict[str, Any], path: list[str | int], rel: str | None) -> None:
    if not path:
        entry["local_path"] = rel
        return
    cursor = entry["value"]
    for step in path[:-1]:
        cursor = cursor[step]
    cursor["local_path"] = rel


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
