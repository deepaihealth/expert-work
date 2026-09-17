"""B-61 Task 2 —— 预拉脚本的判定逻辑(不起沙箱,直接 import)。"""

from __future__ import annotations

import ast
import http.server
import inspect
import json
import os
import socket
import stat
import textwrap
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, ClassVar

import pytest

from orchestrator.tools import prefetch_script
from orchestrator.tools.prefetch_script import (
    CACHE_TTL_S,
    MAX_FILE_BYTES,
    MAX_TOTAL_BYTES,
    cache_digest,
    content_type_ok,
    main,
    pick_suffix,
    script_source,
)


@pytest.mark.parametrize(
    "content_type",
    [
        "image/jpeg",
        "image/png; charset=binary",
        "video/mp4",
        "audio/mpeg",
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/msword",
        "application/vnd.ms-excel",
    ],
)
def test_media_types_are_prefetched(content_type: str) -> None:
    assert content_type_ok(content_type) is True


@pytest.mark.parametrize(
    "content_type",
    ["text/html", "text/html; charset=utf-8", "application/json", "text/plain", ""],
)
def test_pages_and_text_are_not_prefetched(content_type: str) -> None:
    assert content_type_ok(content_type) is False


def test_suffix_prefers_the_content_type_over_the_url() -> None:
    assert pick_suffix("image/jpeg", "https://x/y") == ".jpg"
    assert pick_suffix("video/mp4", "https://x/y.bin") == ".mp4"


def test_suffix_falls_back_to_the_url_extension() -> None:
    assert pick_suffix("application/octet-stream", "https://x/y.webp") == ".webp"


def test_suffix_is_empty_when_neither_says_anything() -> None:
    assert pick_suffix("application/octet-stream", "https://x/y") == ""


def test_limits_are_the_spec_numbers() -> None:
    assert MAX_FILE_BYTES == 32 * 1024 * 1024
    assert MAX_TOTAL_BYTES == 128 * 1024 * 1024


def test_script_source_is_this_module_and_imports_only_stdlib() -> None:
    source = script_source()
    assert "def content_type_ok" in source
    tree = ast.parse(source)
    imported = {
        node.module.split(".")[0]
        if isinstance(node, ast.ImportFrom) and node.module
        else alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in (node.names or [ast.alias(name="")])
    }
    forbidden = {"orchestrator", "expert_work", "control_plane", "httpx", "pydantic"}
    hit = imported & forbidden
    assert not hit, f"脚本要在沙箱里跑,不能依赖仓库/三方包: {hit}"


def test_script_source_matches_the_file_on_disk() -> None:
    path = Path(__file__).parents[1] / "src/orchestrator/tools/prefetch_script.py"
    assert script_source() == path.read_text(encoding="utf-8")


def test_site_walk_matches_the_host_side_implementation() -> None:
    from orchestrator.tools.inputs_doc import iter_url_sites, root_key
    from orchestrator.tools.prefetch_script import _linked_sites, _root_key

    too_deep: Any = {"url": "https://x/g.png"}
    for _ in range(prefetch_script.MAX_PARSE_DEPTH):
        too_deep = [too_deep]

    doc = {
        "variables": {
            "a": {"value": "https://x/1.jpg", "trusted": True},
            "b": {"value": [{"url": "https://x/2.mp4"}, {"url": "not-a-url"}], "trusted": True},
            "c": {
                "value": {
                    "url": "https://ok/a.mp4",
                    "local_path": "https://attacker/b.mp4",
                },
                "trusted": True,
            },
            # 终审 finding 1 —— 列表里的裸 URL(一层 / 两层):旁边没有地方记
            # local_path,两侧都必须判它不是 site。
            "d": {"value": ["https://a"], "trusted": True},
            "e": {"value": [["https://a"]], "trusted": True},
            # B-67 §4.3 —— JSON 字符串:两侧都看 value_parsed,不看 value。
            "f": {
                "value": '[{"url": "https://x/f.pdf"}]',
                "value_parsed": [{"url": "https://x/f.pdf"}],
                "trusted": False,
            },
            # B-67 终审 F2 —— 嵌套超过 MAX_PARSE_DEPTH 层:两侧都不扫;恰好在上限上的照扫。
            "g": {"value": too_deep, "trusted": True},
            "h": {"value": too_deep[0], "trusted": True},
        }
    }
    host = [(s.var_name, list(s.path), s.url) for s in iter_url_sites(doc)]
    # 沙箱侧走 main 真正用的那条路(_linked_sites:深度闸 + _sites)。
    sandbox = [
        (name, path, url)
        for name, entry in doc["variables"].items()
        for path, url, _link in _linked_sites(name, entry[_root_key(entry)])
    ]
    assert host == sandbox
    assert all(_root_key(e) == root_key(e) for e in doc["variables"].values())
    # 显式钉住攻击场景本身:c 只应该命中 url 那条,local_path 绝不能被当成待预拉的地址。
    assert ("c", ["url"], "https://ok/a.mp4") in sandbox
    assert not any(name == "c" and url == "https://attacker/b.mp4" for name, _, url in sandbox)
    # 同样显式钉住 finding 1:d/e 一条 site 都不该有——沙箱侧 _assign 走到那里会
    # TypeError,而 main 的 per-site 兜底之外,首先靠的就是这条判定。
    assert not any(name in {"d", "e"} for name, _, _ in sandbox)
    assert ("f", [0, "url"], "https://x/f.pdf") in sandbox
    assert not any(name == "g" for name, _, _ in sandbox)
    assert [url for name, _, url in sandbox if name == "h"] == ["https://x/g.png"]


# ---------------------------------------------------------------------------
# 下面这组测试起真实的 stdlib HTTP server(或裸 socket),走 main() 的完整路径——
# 覆盖 review 指出的「_fetch / main / budget 全无测试」缺口。之前那组测试只覆盖
# 纯函数,Critical bug(http.client.HTTPException 逃逸)恰好就藏在这段没人测的
# 区域。
# ---------------------------------------------------------------------------


_Routes = dict[str, Callable[["_RoutedHandler"], None]]
_HttpServer = tuple[str, _Routes]


class _RoutedHandler(http.server.BaseHTTPRequestHandler):
    """按路径分发到测试注册的处理函数;未注册的路径一律 404。"""

    routes: ClassVar[_Routes] = {}

    def do_GET(self) -> None:
        handler = self.routes.get(self.path)
        if handler is None:
            self.send_response(404)
            self.end_headers()
            return
        handler(self)

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def http_server() -> Iterator[_HttpServer]:
    """起一个真实的 127.0.0.1 HTTP server;用例往返回的 routes 字典里注册路径。"""
    routes: _Routes = {}

    class Handler(_RoutedHandler):
        pass

    Handler.routes = routes
    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", routes
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def _write_inputs(tmp_path: Path, variables: dict[str, Any]) -> Path:
    run_dir = tmp_path / "inputs" / "run1"
    run_dir.mkdir(parents=True)
    path = run_dir / "inputs.json"
    path.write_text(json.dumps({"run_id": "run1", "variables": variables}), encoding="utf-8")
    return path


def test_fetch_hit_writes_file_and_relative_local_path(
    tmp_path: Path, http_server: _HttpServer
) -> None:
    base, routes = http_server
    body = b"\xff\xd8\xff" + b"A" * 32

    def ok(handler: _RoutedHandler) -> None:
        handler.send_response(200)
        handler.send_header("Content-Type", "image/jpeg")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    routes["/ok.jpg"] = ok
    inputs_path = _write_inputs(
        tmp_path, {"org_logo": {"value": f"{base}/ok.jpg", "trusted": True}}
    )

    assert main(["prefetch_script.py", str(inputs_path)]) == 0

    doc = json.loads(inputs_path.read_text(encoding="utf-8"))
    name = cache_digest(f"{base}/ok.jpg") + ".jpg"
    assert doc["variables"]["org_logo"]["local_path"] == "inputs/run1/org_logo.jpg"
    saved = tmp_path / "inputs" / "cache" / name
    assert saved.read_bytes() == body


def test_fetch_404_is_a_miss(tmp_path: Path, http_server: _HttpServer) -> None:
    base, _routes = http_server
    inputs_path = _write_inputs(
        tmp_path, {"org_logo": {"value": f"{base}/missing.jpg", "trusted": True}}
    )

    assert main(["prefetch_script.py", str(inputs_path)]) == 0

    doc = json.loads(inputs_path.read_text(encoding="utf-8"))
    assert doc["variables"]["org_logo"]["local_path"] is None
    assert not (tmp_path / "inputs" / "cache").exists()


def test_fetch_html_content_type_is_a_miss(tmp_path: Path, http_server: _HttpServer) -> None:
    base, routes = http_server
    body = b"<html></html>"

    def page(handler: _RoutedHandler) -> None:
        handler.send_response(200)
        handler.send_header("Content-Type", "text/html")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    routes["/page"] = page
    inputs_path = _write_inputs(tmp_path, {"link": {"value": f"{base}/page", "trusted": True}})

    assert main(["prefetch_script.py", str(inputs_path)]) == 0

    doc = json.loads(inputs_path.read_text(encoding="utf-8"))
    assert doc["variables"]["link"]["local_path"] is None
    assert not (tmp_path / "inputs" / "cache").exists()


def test_fetch_oversize_without_content_length_is_a_miss(
    tmp_path: Path, http_server: _HttpServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(prefetch_script, "MAX_FILE_BYTES", 8)
    base, routes = http_server
    body = b"X" * 32

    def big(handler: _RoutedHandler) -> None:
        handler.send_response(200)
        handler.send_header("Content-Type", "image/jpeg")
        # 故意不发 Content-Length——逼 _fetch 走「读 cap+1 字节按长度截断」判定。
        handler.end_headers()
        handler.wfile.write(body)

    routes["/big"] = big
    inputs_path = _write_inputs(tmp_path, {"org_logo": {"value": f"{base}/big", "trusted": True}})

    assert main(["prefetch_script.py", str(inputs_path)]) == 0

    doc = json.loads(inputs_path.read_text(encoding="utf-8"))
    assert doc["variables"]["org_logo"]["local_path"] is None
    assert not (tmp_path / "inputs" / "cache").exists()


def test_fetch_declared_length_mismatch_is_a_miss(tmp_path: Path, http_server: _HttpServer) -> None:
    """Finding 2:声明 Content-Length 但提前断连——body 被静默截断,必须当失败处理。"""
    base, routes = http_server

    def truncated(handler: _RoutedHandler) -> None:
        handler.send_response(200)
        handler.send_header("Content-Type", "image/jpeg")
        handler.send_header("Content-Length", "1000")  # 声明 1000,实际只发 10
        handler.end_headers()
        handler.wfile.write(b"only-ten!!")

    routes["/truncated"] = truncated
    inputs_path = _write_inputs(
        tmp_path, {"org_logo": {"value": f"{base}/truncated", "trusted": True}}
    )

    assert main(["prefetch_script.py", str(inputs_path)]) == 0

    doc = json.loads(inputs_path.read_text(encoding="utf-8"))
    assert doc["variables"]["org_logo"]["local_path"] is None
    assert not (tmp_path / "inputs" / "cache").exists()


def test_fetch_malformed_status_line_is_a_miss_and_does_not_crash(tmp_path: Path) -> None:
    """Finding 1:裸 socket 回一行不是 HTTP 状态行的垃圾,urlopen 抛
    ``http.client.BadStatusLine``(只继承 HTTPException,不继承 OSError/URLError)。
    """
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_socket.bind(("127.0.0.1", 0))
    port = server_socket.getsockname()[1]
    server_socket.listen(1)

    def serve_one() -> None:
        conn, _addr = server_socket.accept()
        try:
            conn.recv(4096)
            conn.sendall(b"GARBAGE NOT HTTP\r\n\r\n")
        finally:
            conn.close()

    thread = threading.Thread(target=serve_one, daemon=True)
    thread.start()
    try:
        inputs_path = _write_inputs(
            tmp_path, {"org_logo": {"value": f"http://127.0.0.1:{port}/x", "trusted": True}}
        )
        # 断言本身就是「不崩」:main() 若把异常漏出来,pytest 会把它当测试失败报出。
        assert main(["prefetch_script.py", str(inputs_path)]) == 0
        doc = json.loads(inputs_path.read_text(encoding="utf-8"))
        assert doc["variables"]["org_logo"]["local_path"] is None
    finally:
        thread.join(timeout=5)
        server_socket.close()


def test_budget_exhausted_by_first_file_refuses_second(
    tmp_path: Path, http_server: _HttpServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(prefetch_script, "MAX_TOTAL_BYTES", 20)
    monkeypatch.setattr(prefetch_script, "MAX_FILE_BYTES", 100)
    base, routes = http_server
    first_body = b"F" * 15
    second_body = b"S" * 10

    def first(handler: _RoutedHandler) -> None:
        handler.send_response(200)
        handler.send_header("Content-Type", "image/jpeg")
        handler.send_header("Content-Length", str(len(first_body)))
        handler.end_headers()
        handler.wfile.write(first_body)

    def second(handler: _RoutedHandler) -> None:
        handler.send_response(200)
        handler.send_header("Content-Type", "image/png")
        handler.send_header("Content-Length", str(len(second_body)))
        handler.end_headers()
        handler.wfile.write(second_body)

    routes["/first"] = first
    routes["/second"] = second
    inputs_path = _write_inputs(
        tmp_path,
        {
            "a": {"value": f"{base}/first", "trusted": True},
            "b": {"value": f"{base}/second", "trusted": True},
        },
    )

    assert main(["prefetch_script.py", str(inputs_path)]) == 0

    doc = json.loads(inputs_path.read_text(encoding="utf-8"))
    assert doc["variables"]["a"]["local_path"] == "inputs/run1/a"
    assert doc["variables"]["b"]["local_path"] is None


def test_main_survives_unparseable_inputs_json(tmp_path: Path) -> None:
    """Finding 4:inputs.json 存在但解析不了(比如续跑时被沙箱代码写坏了)。"""
    run_dir = tmp_path / "inputs" / "run1"
    run_dir.mkdir(parents=True)
    path = run_dir / "inputs.json"
    path.write_text("{not valid json", encoding="utf-8")

    assert main(["prefetch_script.py", str(path)]) == 0
    # 解析失败时不碰文件——不把半份坏 JSON 覆盖成看起来更「正常」的空文档。
    assert path.read_text(encoding="utf-8") == "{not valid json"


def test_main_survives_missing_inputs_json(tmp_path: Path) -> None:
    missing = tmp_path / "inputs" / "run1" / "inputs.json"
    assert main(["prefetch_script.py", str(missing)]) == 0


def _route(
    body: bytes, content_type: str = "image/jpeg", served: list[str] | None = None
) -> Callable[[_RoutedHandler], None]:
    """一条回固定 body 的 200 路由。

    ``served`` 给了就记下每一次**真正打到 server** 的请求 —— 「命中缓存」的判据就是
    这里没有被叫到。
    """

    def route(handler: _RoutedHandler) -> None:
        if served is not None:
            served.append(handler.path)
        handler.send_response(200)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    return route


def test_a_bare_url_in_a_list_does_not_lose_the_other_variables(
    tmp_path: Path, http_server: _HttpServer
) -> None:
    """终审 finding 1 —— ``{"images": ["https://a"]}`` 以前会让 ``_assign`` 抛
    ``TypeError``:``main`` 的 per-site 循环没有任何处理,脚本非 0 退出,结尾那次
    ``json.dump`` 根本不跑 —— **每个**变量的 ``local_path`` 全丢,已经下载好的文件
    躺在 ``inputs/cache/`` 里却没有一条记录指向它们。列表里的裸 URL 现在不算 site,
    后面的变量照常回填。"""
    base, routes = http_server
    body = b"\xff\xd8\xff" + b"A" * 16
    routes["/ok.jpg"] = _route(body)
    inputs_path = _write_inputs(
        tmp_path,
        {
            "images": {"value": [f"{base}/ok.jpg"], "trusted": True},
            "org_logo": {"value": f"{base}/ok.jpg", "trusted": True},
        },
    )

    assert main(["prefetch_script.py", str(inputs_path)]) == 0

    doc = json.loads(inputs_path.read_text(encoding="utf-8"))
    name = cache_digest(f"{base}/ok.jpg") + ".jpg"
    assert doc["variables"]["org_logo"]["local_path"] == "inputs/run1/org_logo.jpg"
    assert (tmp_path / "inputs" / "cache" / name).read_bytes() == body
    # 列表里的裸 URL 原样留着(没被回填、也没把结构改成别的形状)。
    assert doc["variables"]["images"]["value"] == [f"{base}/ok.jpg"]


def test_main_survives_a_document_that_is_not_an_object(tmp_path: Path) -> None:
    """终审 finding 1 —— ``inputs.json`` 里躺着一个 list(沙箱代码写坏 / 续跑)时
    ``doc.get`` 直接 ``AttributeError``,整轮预拉丢掉。形状不对就当没东西可拉。"""
    run_dir = tmp_path / "inputs" / "run1"
    run_dir.mkdir(parents=True)
    path = run_dir / "inputs.json"
    path.write_text('["not", "a", "document"]', encoding="utf-8")

    assert main(["prefetch_script.py", str(path)]) == 0
    assert path.read_text(encoding="utf-8") == '["not", "a", "document"]'


def test_each_site_is_persisted_before_the_next_one(
    tmp_path: Path, http_server: _HttpServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """终审 finding 3 —— 整段 exec 有墙钟上限,超了被 SIGKILL;文档只在最后写一次
    的话,被杀等于**一个** ``local_path`` 都没落下。逐 site 原子改写后,已经拉完的
    那些在文件里。这里用「第二个 site 上抛 ``KeyboardInterrupt``」模拟进程被抬走
    (``BaseException``,per-site 的 ``except Exception`` 抓不住,等价于没有兜底)。
    """
    base, routes = http_server
    body = b"\xff\xd8\xff" + b"A" * 16
    routes["/a.jpg"] = _route(body)
    routes["/b.jpg"] = _route(body)
    inputs_path = _write_inputs(
        tmp_path,
        {
            "a": {"value": f"{base}/a.jpg", "trusted": True},
            "b": {"value": f"{base}/b.jpg", "trusted": True},
        },
    )
    real_fetch = prefetch_script._fetch
    calls: list[int] = []

    def fetch(*args: Any, **kwargs: Any) -> tuple[str | None, int]:
        calls.append(1)
        if len(calls) == 2:
            raise KeyboardInterrupt
        return real_fetch(*args, **kwargs)

    monkeypatch.setattr(prefetch_script, "_fetch", fetch)

    with pytest.raises(KeyboardInterrupt):
        main(["prefetch_script.py", str(inputs_path)])

    doc = json.loads(inputs_path.read_text(encoding="utf-8"))
    assert doc["variables"]["a"]["local_path"] == "inputs/run1/a.jpg"


def test_one_failing_site_does_not_lose_the_others(
    tmp_path: Path, http_server: _HttpServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """终审 finding 1 —— per-site 兜底:一个 site 的任何意外(这里用落盘失败模拟)
    只让它自己算 miss,不能带走其它 site 已经拿到的结果。"""
    base, routes = http_server
    body = b"\xff\xd8\xff" + b"A" * 16
    routes["/a.jpg"] = _route(body)
    routes["/b.jpg"] = _route(body)
    inputs_path = _write_inputs(
        tmp_path,
        {
            "a": {"value": f"{base}/a.jpg", "trusted": True},
            "b": {"value": f"{base}/b.jpg", "trusted": True},
        },
    )
    real_rewrite = prefetch_script._rewrite
    calls: list[int] = []

    def rewrite(path: str, doc: dict[str, Any]) -> None:
        calls.append(1)
        if len(calls) == 1:
            msg = "no space left on device"
            raise OSError(msg)
        real_rewrite(path, doc)

    monkeypatch.setattr(prefetch_script, "_rewrite", rewrite)

    assert main(["prefetch_script.py", str(inputs_path)]) == 0

    doc = json.loads(inputs_path.read_text(encoding="utf-8"))
    assert doc["variables"]["b"]["local_path"] == "inputs/run1/b.jpg"


# ---------------------------------------------------------------------------
# T11 —— 内容寻址的共享缓存:同一个 URL 跨轮只下一次,文件名只由 URL 的 sha256 定。
# ---------------------------------------------------------------------------


def test_cache_digest_is_stable_and_url_keyed() -> None:
    first = cache_digest("https://x/a.jpg")

    assert first == cache_digest("https://x/a.jpg")
    assert first != cache_digest("https://x/b.jpg")
    assert len(first) == 32
    assert "/" not in first and "." not in first


def test_second_fetch_of_the_same_url_reuses_the_cache_without_a_request(
    tmp_path: Path, http_server: _HttpServer
) -> None:
    """同一个 URL 第二次:命中缓存,server 不该再收到请求,预算也不该被扣。"""
    base, routes = http_server
    body = b"\xff\xd8\xff" + b"A" * 32
    served: list[str] = []
    routes["/ok.jpg"] = _route(body, served=served)
    url = f"{base}/ok.jpg"
    cache_dir = str(tmp_path / "cache")

    first_name, first_used = prefetch_script._fetch(url, cache_dir, MAX_TOTAL_BYTES)
    hits = [len(served)]
    second_name, second_used = prefetch_script._fetch(url, cache_dir, MAX_TOTAL_BYTES)
    hits.append(len(served))

    assert first_name == cache_digest(url) + ".jpg"
    assert second_name == first_name
    assert first_used == len(body)
    assert hits == [1, 1]  # server 端计数:第二次没有新请求
    assert second_used == 0  # 命中不扣预算


def test_a_url_without_an_extension_still_hits_on_the_second_fetch(
    tmp_path: Path, http_server: _HttpServer
) -> None:
    """探针必须按 digest 前缀扫目录, 不能拼 URL 扩展名。

    ``/logo`` 这种没有扩展名的 URL, 响应 ``image/png`` 会落成 ``<digest>.png``;
    按 URL 扩展名去探就是探 ``<digest>``(空后缀)——永久不命中且每轮重下,
    这个任务等于白做。这条是该错法的实证。
    """
    base, routes = http_server
    body = b"\x89PNG\r\n\x1a\n" + b"A" * 16
    served: list[str] = []
    routes["/logo"] = _route(body, "image/png", served)
    url = f"{base}/logo"
    cache_dir = str(tmp_path / "cache")

    first_name, _first_used = prefetch_script._fetch(url, cache_dir, MAX_TOTAL_BYTES)
    second_name, second_used = prefetch_script._fetch(url, cache_dir, MAX_TOTAL_BYTES)

    # 后缀只能来自 content-type:URL 自己一个扩展名都没有。
    assert first_name == cache_digest(url) + ".png"
    assert second_name == first_name
    assert len(served) == 1
    assert second_used == 0


def test_an_expired_cache_entry_is_refetched(tmp_path: Path, http_server: _HttpServer) -> None:
    """条目超过 ``CACHE_TTL_S`` 就重下 —— 换掉的 logo 最迟一天后会被拿到新版本。"""
    base, routes = http_server
    body = b"\xff\xd8\xff" + b"A" * 32
    served: list[str] = []
    routes["/ok.jpg"] = _route(body, served=served)
    url = f"{base}/ok.jpg"
    cache_dir = str(tmp_path / "cache")

    name, _used = prefetch_script._fetch(url, cache_dir, MAX_TOTAL_BYTES)
    assert name is not None
    cached = os.path.join(cache_dir, name)
    stale = time.time() - CACHE_TTL_S - 60
    os.utime(cached, (stale, stale))

    again, used = prefetch_script._fetch(url, cache_dir, MAX_TOTAL_BYTES)

    assert again == name  # 内容寻址:重下盖回同一个条目,不是再添一个
    assert len(served) == 2  # 真的又发了一次请求
    assert used == len(body)  # 重下的字节照扣预算
    # mtime 只在下载时刷新(命中不 touch),所以它就是「最近一次下载时间」。
    assert os.stat(cached).st_mtime > stale


def test_a_cached_file_is_group_and_world_readable(
    tmp_path: Path, http_server: _HttpServer
) -> None:
    """落盘权限位必须是 0644。

    并发 run 撞同一 URL 要用唯一临时文件(``mkstemp``), 而 ``mkstemp`` 恒 0600、
    ``os.replace`` 保权限位 —— 不显式 chmod 就会把今天 ``open()`` 写出来的 0644
    静默降成 0600, 跨 uid 的读方(W2-BUG-1 的原病)再次读不到。
    """
    base, routes = http_server
    body = b"\xff\xd8\xff" + b"A" * 32
    routes["/ok.jpg"] = _route(body)
    cache_dir = str(tmp_path / "cache")

    name, _used = prefetch_script._fetch(f"{base}/ok.jpg", cache_dir, MAX_TOTAL_BYTES)
    assert name is not None
    cached = os.path.join(cache_dir, name)

    assert stat.S_IMODE(os.stat(cached).st_mode) == 0o644


def test_an_interrupted_write_never_leaves_a_partial_cache_entry(tmp_path: Path) -> None:
    """落盘走「同目录临时文件 + ``os.replace``」:目标路径上要么是完整文件,要么是上一版。

    直接 ``open(目标, "wb")`` 写的话,``open`` 自己就先把已有条目截成 0 字节 ——
    随后写入被打断(盘满 / 进程被抬走)就留下一个半截文件,而它下一轮会被当成
    命中,直接喂给模型。这里把失败精确地卡在「``open`` 之后、内容落盘之前」
    (body 不是 bytes),看的就是目标路径有没有被动过。
    """
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    entry = cache_dir / ("a" * 32 + ".jpg")
    entry.write_bytes(b"COMPLETE-OLD-ENTRY")

    with pytest.raises(TypeError):
        prefetch_script._store(str(cache_dir), entry.name, "not-bytes")  # type: ignore[arg-type]

    assert entry.read_bytes() == b"COMPLETE-OLD-ENTRY"
    assert list(cache_dir.iterdir()) == [entry]  # 也没把临时文件留在配额目录里


@pytest.mark.parametrize("stale_first", [True, False])
def test_a_stale_sibling_never_shadows_the_fresh_entry(tmp_path: Path, stale_first: bool) -> None:
    """同一个 digest 的超期兄弟条目必须被跳过并删掉,不能把新鲜的那个遮住。

    同一个 URL 的后缀会随响应变(``/logo`` 今天 ``image/png``、明天 ``image/jpeg``;
    CDN 偶尔回 ``application/octet-stream`` 就回落到 URL 扩展名),而 ``os.replace``
    只盖同名的那个 —— 目录里于是同时躺着一个超期的和一个新鲜的。探针一碰到超期的就
    ``None`` 返回的话,``os.scandir`` 的文件系统顺序说了算:超期那个先被扫到就永远
    命不中,每轮重下、每轮再写一份,正好退回本任务要治的病。

    两条断言合起来与扫描顺序无关:先扫到超期的 → 返回值错;先扫到新鲜的 → 超期的
    没被删掉。创建顺序两种都跑一遍只是再加一层。
    """
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    digest = cache_digest("https://x/logo")
    stale = cache_dir / f"{digest}.png"
    fresh = cache_dir / f"{digest}.jpg"
    for path in [stale, fresh] if stale_first else [fresh, stale]:
        path.write_bytes(b"x")
    old = time.time() - CACHE_TTL_S - 60
    os.utime(stale, (old, old))

    assert prefetch_script._cached_name(str(cache_dir), digest) == fresh.name
    assert not stale.exists()  # 顺手清掉,不然孤儿要等回收闸


def test_a_cache_hit_never_touches_the_entry(tmp_path: Path, http_server: _HttpServer) -> None:
    """命中缓存**不动 mtime** —— 这条不变式此前只有注释在守。

    mtime 的语义是「最近一次下载时间」,T12 的回收闸按它过期。命中时 touch 一下,
    热条目就永远不会超过 ``CACHE_TTL_S``、也就永远不重下,一个被换掉的 logo 会被
    无限期地喂给模型 —— B-61 的起因正是这个。
    """
    base, routes = http_server
    body = b"\xff\xd8\xff" + b"A" * 32
    served: list[str] = []
    routes["/ok.jpg"] = _route(body, served=served)
    url = f"{base}/ok.jpg"
    cache_dir = str(tmp_path / "cache")

    name, _used = prefetch_script._fetch(url, cache_dir, MAX_TOTAL_BYTES)
    assert name is not None
    cached = os.path.join(cache_dir, name)
    before = os.stat(cached).st_mtime_ns

    hit_name, hit_used = prefetch_script._fetch(url, cache_dir, MAX_TOTAL_BYTES)

    assert (hit_name, hit_used) == (name, 0)  # 先确认这一次真的走的是命中路径
    assert len(served) == 1
    assert os.stat(cached).st_mtime_ns == before


# ---------------------------------------------------------------------------
# B-67 Task 2 —— §4.1 按变量名建符号链接
# ---------------------------------------------------------------------------


def _wrapped(inner: Any, levels: int) -> Any:
    for _ in range(levels):
        inner = [inner]
    return inner


_LONG_DESCRIPTION = "长" * 30 + "abcdefghijklmnopqrstuvwxyz"  # 56 字,slug 只取前 40
_AT_DEPTH_BOUND = _wrapped({"url": "https://x/b.png"}, prefetch_script.MAX_PARSE_DEPTH - 1)

#: ``(变量名, 调用方给的原始值, 期望的链接名)``。前六行是基本形状;其余每行专门照出一种
#: 现实的漂移(slug 截断长度、扩展名的大小写 / 长度、按整条 URL 取后缀、保留名、顺延撞名、
#: 深度闸),两侧任何一侧改了这些,至少一行会红。
_NAME_CORPUS: list[tuple[str, Any, list[str]]] = [
    ("org_logo", "https://x/cover-1726394851207.png", ["org_logo.png"]),
    ("brand", {"logo": "https://x/l.jpg", "name": "深护"}, ["brand.logo.jpg"]),
    (
        "materials",
        '[{"description": "示范 视频", "url": "https://x/a.mp4"}, {"description": "无链接"},'
        ' {"url": "https://x/c.pdf", "thumb": "https://x/c.pdf"}]',
        ["materials/0-示范视频.mp4", "materials/2.pdf", "materials/2-2.pdf"],
    ),
    ("page", "https://x/post", ["page"]),
    ("nested", {"a": [{"url": "https://x/n.png", "description": "..x"}]}, ["nested.a/0-x.png"]),
    ("note", "短文本", []),
    (
        "long_desc",
        [{"description": _LONG_DESCRIPTION, "url": "https://x/l.png"}],
        [f"long_desc/0-{_LONG_DESCRIPTION[:40]}.png"],
    ),
    ("empty_key", {"": "https://x/e.png"}, ["empty_key._.png"]),
    ("upper_ext", "https://x/A.PNG", ["upper_ext.PNG"]),
    ("ext4", "https://x/deck.pptx", ["ext4.pptx"]),
    ("ext5", "https://x/page.xhtml", ["ext5.xhtml"]),
    ("ext6", "https://x/a.abcdef", ["ext6"]),
    ("signed", "https://x/a.png?sig=abc.def&e=1#frag.x", ["signed.png"]),
    ("desc_not_str", [{"description": 42, "url": "https://x/d.png"}], ["desc_not_str/0.png"]),
    ("inputs", "https://x/i.json", ["inputs-2.json"]),
    ("inputs", {"json": "https://x/i.tmp"}, ["inputs.json-2.tmp"]),
    (
        "dup",
        {
            "a": "https://x/1.png",
            "a!": "https://x/2.png",
            "a-2": "https://x/3.png",
            "a?": "https://x/4.png",
        },
        ["dup.a.png", "dup.a-2.png", "dup.a-2-2.png", "dup.a-3.png"],
    ),
    ("at_bound", _AT_DEPTH_BOUND, ["at_bound/0.png"]),
    ("too_deep", [_AT_DEPTH_BOUND], []),
    ("too_deep_json", json.dumps([_AT_DEPTH_BOUND]), []),
]


@pytest.mark.parametrize(
    ("name", "value", "expected"),
    _NAME_CORPUS,
    ids=[f"{i}-{row[0]}" for i, row in enumerate(_NAME_CORPUS)],
)
def test_link_names_match_the_host_side_implementation(
    name: str, value: Any, expected: list[str]
) -> None:
    """链接名三处同义的钉子(行为面):宿主 ``inputs_doc.linked_sites``(对原始值)与沙箱
    ``_linked_sites``(对清单里的那一份,main 真正走的路)必须给出同一组 site 与名字 ——
    提示词里说的名字就是预拉建出来的名字。算法本身逐字同义另由
    ``test_copied_helpers_are_the_host_algorithm`` 钉。"""
    from uuid import UUID

    from expert_work.protocol import PromptVariableSpec
    from orchestrator.tools.inputs_doc import build_inputs_doc, linked_sites
    from orchestrator.tools.prefetch_script import _linked_sites, _root_key

    doc = build_inputs_doc(
        run_id=UUID(int=1),
        variables=[PromptVariableSpec(name=name, required=False)],
        inputs={name: value},
    )
    assert doc is not None
    entry = doc["variables"][name]

    host = [(list(s.site.path), s.site.url, s.link) for s in linked_sites(name, value)]
    sandbox = _linked_sites(name, entry[_root_key(entry)])
    assert host == sandbox
    assert [link for _path, _url, link in sandbox] == expected


#: 宿主 → 沙箱的**刻意**改名:宿主的公开 helper 在沙箱里带下划线;沙箱不 import
#: ``collections.abc``,isinstance 用具体类型。除此之外一个字都不许不同。
_HOST_HELPER_RENAMES = {"url_suffix": "_url_suffix", "slugify": "_slugify"}
_HOST_ISINSTANCE_RENAMES = {"Mapping": "dict", "Sequence": "list"}

#: ``(宿主函数名, 沙箱函数名)``:沙箱里逐字复制的全部 helper。
_COPIED_HELPERS = [
    ("url_suffix", "_url_suffix"),
    ("slugify", "_slugify"),
    ("site_description", "_site_description"),
    ("_link_stem", "_link_stem"),
    ("link_names", "_link_names"),
    ("root_key", "_root_key"),
    ("_too_deep", "_too_deep"),
]


def _algorithm_dump(fn: Callable[..., Any], *, host: bool) -> str:
    """函数的语法树:去掉 docstring、函数名、参数 / 返回注解;宿主侧再套上面两张改名表
    (isinstance 那张只改 isinstance 的第二个参数)。函数体里其余的一切都留着比。"""
    func = ast.parse(textwrap.dedent(inspect.getsource(fn))).body[0]
    assert isinstance(func, ast.FunctionDef)
    if ast.get_docstring(func) is not None:
        func.body = func.body[1:]
    func.name = "_"
    func.returns = None
    for arg in (*func.args.posonlyargs, *func.args.args, *func.args.kwonlyargs):
        arg.annotation = None
    if host:
        for node in ast.walk(func):
            if isinstance(node, ast.Name) and node.id in _HOST_HELPER_RENAMES:
                node.id = _HOST_HELPER_RENAMES[node.id]
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "isinstance"
                and len(node.args) == 2
                and isinstance(node.args[1], ast.Name)
                and node.args[1].id in _HOST_ISINSTANCE_RENAMES
            ):
                node.args[1].id = _HOST_ISINSTANCE_RENAMES[node.args[1].id]
    return ast.dump(func)


@pytest.mark.parametrize(("host_name", "sandbox_name"), _COPIED_HELPERS)
def test_copied_helpers_are_the_host_algorithm(host_name: str, sandbox_name: str) -> None:
    """链接名三处同义的钉子(算法面):行为语料只能覆盖它想到的输入,改一个常量或把
    ``urlparse(url).path`` 换成 ``url`` 这类漂移可以在语料上恰好不露。这里逐个比函数的
    语法树。"""
    from orchestrator.tools import inputs_doc

    host = _algorithm_dump(getattr(inputs_doc, host_name), host=True)
    sandbox = _algorithm_dump(getattr(prefetch_script, sandbox_name), host=False)
    assert sandbox == host


def test_copied_constants_match_the_host_side() -> None:
    from orchestrator.tools import inputs_doc

    for name in ("PARSED_KEY", "SLUG_MAX_CHARS", "MAX_PARSE_DEPTH", "_RESERVED_NAMES"):
        assert getattr(prefetch_script, name) == getattr(inputs_doc, name), name
    for name in ("_SLUG_DROP", "_EXT_RE"):
        host_re, sandbox_re = getattr(inputs_doc, name), getattr(prefetch_script, name)
        assert (sandbox_re.pattern, sandbox_re.flags) == (host_re.pattern, host_re.flags), name


def test_hit_links_the_file_under_its_variable_name_and_points_local_path_at_the_link(
    tmp_path: Path, http_server: _HttpServer
) -> None:
    base, routes = http_server
    body = b"\xff\xd8\xff" + b"A" * 32
    url = f"{base}/cover-1726394851207.jpg"
    routes["/cover-1726394851207.jpg"] = _route(body)
    inputs_path = _write_inputs(tmp_path, {"org_logo": {"value": url, "trusted": True}})

    assert main(["prefetch_script.py", str(inputs_path)]) == 0

    doc = json.loads(inputs_path.read_text(encoding="utf-8"))
    assert doc["variables"]["org_logo"]["local_path"] == "inputs/run1/org_logo.jpg"
    link = tmp_path / "inputs" / "run1" / "org_logo.jpg"
    assert link.is_symlink()
    assert os.readlink(link) == f"../cache/{cache_digest(url)}.jpg"  # 相对目标,不跨出 inputs/
    assert link.read_bytes() == body  # 从 run 目录出发能解析
    assert (tmp_path / "inputs" / "cache" / f"{cache_digest(url)}.jpg").read_bytes() == body


def test_list_items_link_under_a_variable_directory_using_value_parsed(
    tmp_path: Path, http_server: _HttpServer
) -> None:
    base, routes = http_server
    body = b"\x00\x00\x00\x18ftypmp42" + b"B" * 16
    routes["/a.mp4"] = _route(body, content_type="video/mp4")
    items = [{"description": "示范视频", "url": f"{base}/a.mp4"}, {"description": "无链接"}]
    raw = json.dumps(items, ensure_ascii=False)
    inputs_path = _write_inputs(
        tmp_path, {"materials": {"value": raw, "value_parsed": items, "trusted": False}}
    )

    assert main(["prefetch_script.py", str(inputs_path)]) == 0

    entry = json.loads(inputs_path.read_text(encoding="utf-8"))["variables"]["materials"]
    assert entry["value"] == raw  # 字符串一个字不动
    assert entry["value_parsed"][0]["local_path"] == "inputs/run1/materials/0-示范视频.mp4"
    assert "local_path" not in entry["value_parsed"][1]
    link = tmp_path / "inputs" / "run1" / "materials" / "0-示范视频.mp4"
    assert link.is_symlink()
    assert os.readlink(link).startswith("../../cache/")
    assert link.read_bytes() == body


def test_rerun_replaces_the_link_instead_of_failing_on_file_exists(
    tmp_path: Path, http_server: _HttpServer
) -> None:
    """续跑 / 同一 run 再预拉:``os.symlink`` 到已存在路径会 ``FileExistsError``,先删后建。"""
    base, routes = http_server
    routes["/l.png"] = _route(b"\x89PNG" + b"C" * 16, content_type="image/png")
    inputs_path = _write_inputs(tmp_path, {"logo": {"value": f"{base}/l.png", "trusted": True}})

    assert main(["prefetch_script.py", str(inputs_path)]) == 0
    assert main(["prefetch_script.py", str(inputs_path)]) == 0

    link = tmp_path / "inputs" / "run1" / "logo.png"
    assert link.is_symlink()
    doc = json.loads(inputs_path.read_text(encoding="utf-8"))
    assert doc["variables"]["logo"]["local_path"] == "inputs/run1/logo.png"


def test_symlink_failure_falls_back_to_the_cache_path(
    tmp_path: Path, http_server: _HttpServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """建不了链接(文件系统不支持)→ ``local_path`` 退回 cache 路径,老消费方无感;不让 run 失败。"""
    base, routes = http_server
    routes["/l.png"] = _route(b"\x89PNG" + b"C" * 16, content_type="image/png")
    inputs_path = _write_inputs(tmp_path, {"logo": {"value": f"{base}/l.png", "trusted": True}})

    def refuse(*args: object, **kwargs: object) -> None:
        raise OSError("symlinks not supported")

    monkeypatch.setattr(os, "symlink", refuse)
    assert main(["prefetch_script.py", str(inputs_path)]) == 0

    doc = json.loads(inputs_path.read_text(encoding="utf-8"))
    name = cache_digest(f"{base}/l.png") + ".png"
    assert doc["variables"]["logo"]["local_path"] == f"inputs/cache/{name}"
    assert not (tmp_path / "inputs" / "run1" / "logo.png").exists()


# ---------------------------------------------------------------------------
# B-67 PR1 终审 —— 链接名不占平台文件名(F1)/ 不越出 run 目录(F6)/ miss 清旧链接(F4)
# / 顺延不撞名(F5)/ 深嵌套(F2)
# ---------------------------------------------------------------------------


def test_sandbox_link_names_skip_the_manifest_names_and_names_already_taken() -> None:
    from orchestrator.tools.prefetch_script import _link_names

    assert _link_names("inputs", [([], "https://x/a.json", None)]) == ["inputs-2.json"]
    assert _link_names("inputs", [(["json"], "https://x/b.tmp", None)]) == ["inputs.json-2.tmp"]
    sites: list[tuple[list[str | int], str, str | None]] = [
        (["a"], "https://x/1.png", None),
        (["a!"], "https://x/2.png", None),
        (["a-2"], "https://x/3.png", None),
        (["a?"], "https://x/4.png", None),
    ]
    assert _link_names("v", sites) == ["v.a.png", "v.a-2.png", "v.a-2-2.png", "v.a-3.png"]


def test_reserved_names_are_the_files_the_script_writes_and_match_the_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """保留名必须恰好是脚本在 run 目录里自己写的文件:清单(宿主给的路径,文件名来自
    ``inputs_abs_path``)与 ``_rewrite`` 的临时文件。改了临时文件后缀却没改保留名,
    链接就又能占到它。"""
    from uuid import uuid4

    from orchestrator.tools import inputs_doc

    written: list[str] = []
    real_replace = os.replace

    def spy(src: str, dst: str) -> None:
        written.extend([os.path.basename(src), os.path.basename(dst)])
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy)
    manifest = tmp_path / os.path.basename(inputs_doc.inputs_abs_path(uuid4()))
    prefetch_script._rewrite(str(manifest), {"variables": {}})

    assert set(written) == prefetch_script._RESERVED_NAMES
    assert prefetch_script._RESERVED_NAMES == inputs_doc._RESERVED_NAMES


@pytest.mark.parametrize("shape", ["top_level_json", "dict_key_json_tmp"])
def test_a_variable_named_inputs_never_touches_the_manifest(
    tmp_path: Path, http_server: _HttpServer, shape: str
) -> None:
    """变量名 ``inputs`` 合法。``inputs`` + ``.json`` 曾让 ``_link`` 删掉清单、``local_path``
    指回清单;``inputs.json`` + ``.tmp`` 曾让 ``_rewrite`` 顺着链接把**整份清单**(所有变量
    的值)写进按 agent 共享的缓存条目 —— 同 agent 下一个 run 引用同一 URL 就命中缓存、
    拿到上一个 run 的清单(7 天内),跨客户泄漏。"""
    base, routes = http_server
    body = b"\x89PNG" + b"D" * 16
    routes["/a.json"] = _route(body, content_type="image/png")
    routes["/b.tmp"] = _route(body, content_type="image/png")
    if shape == "top_level_json":
        url = f"{base}/a.json"
        value: Any = url
        link = "inputs-2.json"
    else:
        url = f"{base}/b.tmp"
        value = {"json": url}
        link = "inputs.json-2.tmp"
    inputs_path = _write_inputs(
        tmp_path,
        {
            "inputs": {"value": value, "trusted": True},
            "other": {"value": "other var value", "trusted": False},
        },
    )

    assert main(["prefetch_script.py", str(inputs_path)]) == 0

    run_dir = tmp_path / "inputs" / "run1"
    # 清单仍是普通文件,内容是完整文档。
    assert not inputs_path.is_symlink()
    assert inputs_path.is_file()
    doc = json.loads(inputs_path.read_text(encoding="utf-8"))
    assert doc["run_id"] == "run1"
    assert doc["variables"]["other"] == {"value": "other var value", "trusted": False}
    assert not os.path.lexists(run_dir / "inputs.json.tmp")
    # 共享缓存条目是下载下来的内容,不是清单。
    assert (tmp_path / "inputs" / "cache" / f"{cache_digest(url)}.png").read_bytes() == body
    # 本变量链到顺延后的名字上,local_path 指向它。
    entry = doc["variables"]["inputs"]
    holder = entry if shape == "top_level_json" else entry["value"]
    assert holder["local_path"] == f"inputs/run1/{link}"
    assert (run_dir / link).is_symlink()
    assert (run_dir / link).read_bytes() == body


def test_an_existing_regular_file_at_the_link_name_is_never_deleted(
    tmp_path: Path, http_server: _HttpServer
) -> None:
    """链接名上已经躺着一个普通文件(沙箱代码写的):那不是我们建的链接,不删;
    ``local_path`` 退回 cache 路径。"""
    base, routes = http_server
    url = f"{base}/l.png"
    routes["/l.png"] = _route(b"\x89PNG" + b"E" * 16, content_type="image/png")
    inputs_path = _write_inputs(tmp_path, {"logo": {"value": url, "trusted": True}})
    squatter = tmp_path / "inputs" / "run1" / "logo.png"
    squatter.write_bytes(b"agent-owned")

    assert main(["prefetch_script.py", str(inputs_path)]) == 0

    assert not squatter.is_symlink()
    assert squatter.read_bytes() == b"agent-owned"
    doc = json.loads(inputs_path.read_text(encoding="utf-8"))
    assert doc["variables"]["logo"]["local_path"] == f"inputs/cache/{cache_digest(url)}.png"


def _plant_outside_dir(tmp_path: Path, victim_kind: str) -> Path:
    """沙箱代码把 ``run1/m`` 换成指向 run 目录之外的目录链接;按 ``victim_kind`` 在那边
    的 ``0-x.png`` 位置放一个普通文件 / 一个链接 / 什么都不放。返回那个位置。"""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "real.txt").write_text("agent-owned", encoding="utf-8")
    victim = outside / "0-x.png"
    if victim_kind == "file":
        victim.write_bytes(b"agent-owned, outside the run dir")
    elif victim_kind == "symlink":
        victim.symlink_to("real.txt")
    (tmp_path / "inputs" / "run1" / "m").symlink_to(outside, target_is_directory=True)
    return victim


def _assert_victim_untouched(victim: Path, victim_kind: str) -> None:
    if victim_kind == "file":
        assert not victim.is_symlink()
        assert victim.read_bytes() == b"agent-owned, outside the run dir"
    elif victim_kind == "symlink":
        assert os.readlink(victim) == "real.txt"
    else:
        assert not os.path.lexists(victim)


@pytest.mark.parametrize("victim_kind", ["absent", "file", "symlink"])
def test_a_planted_directory_link_never_leads_the_script_out_of_the_run_dir(
    tmp_path: Path, http_server: _HttpServer, victim_kind: str
) -> None:
    """跟着沙箱代码种下的目录链接去删 / 建,就是在 run 目录之外删文件、放链接。链接所在
    目录解析后不在 run 目录里 → 不碰,``local_path`` 退回 cache 路径。"""
    base, routes = http_server
    url = f"{base}/e.png"
    routes["/e.png"] = _route(b"\x89PNG" + b"F" * 16, content_type="image/png")
    inputs_path = _write_inputs(
        tmp_path, {"m": {"value": [{"description": "x", "url": url}], "trusted": True}}
    )
    victim = _plant_outside_dir(tmp_path, victim_kind)

    assert main(["prefetch_script.py", str(inputs_path)]) == 0

    _assert_victim_untouched(victim, victim_kind)
    doc = json.loads(inputs_path.read_text(encoding="utf-8"))
    assert doc["variables"]["m"]["value"][0]["local_path"] == (
        f"inputs/cache/{cache_digest(url)}.png"
    )


def test_a_miss_removes_the_stale_link_an_earlier_prefetch_left(
    tmp_path: Path, http_server: _HttpServer
) -> None:
    """同一 run 再预拉(续跑)、这次没拉到:更早那次建的链接要删掉,不能与 null 的
    ``local_path`` 并存 —— 模型按名字找过去,拿到的是上一次的文件。"""
    base, routes = http_server
    url = f"{base}/l.png"
    routes["/l.png"] = _route(b"\x89PNG" + b"G" * 16, content_type="image/png")
    inputs_path = _write_inputs(tmp_path, {"logo": {"value": url, "trusted": True}})
    assert main(["prefetch_script.py", str(inputs_path)]) == 0
    link = tmp_path / "inputs" / "run1" / "logo.png"
    assert link.is_symlink()

    # 下一次:缓存条目没了、源站也 404 → miss。
    (tmp_path / "inputs" / "cache" / f"{cache_digest(url)}.png").unlink()
    del routes["/l.png"]
    assert main(["prefetch_script.py", str(inputs_path)]) == 0

    doc = json.loads(inputs_path.read_text(encoding="utf-8"))
    assert doc["variables"]["logo"]["local_path"] is None
    assert not os.path.lexists(link)


def test_a_miss_never_deletes_a_regular_file_at_the_link_name(
    tmp_path: Path, http_server: _HttpServer
) -> None:
    base, _routes = http_server
    inputs_path = _write_inputs(
        tmp_path, {"logo": {"value": f"{base}/missing.png", "trusted": True}}
    )
    squatter = tmp_path / "inputs" / "run1" / "logo.png"
    squatter.write_bytes(b"agent-owned")

    assert main(["prefetch_script.py", str(inputs_path)]) == 0

    assert not squatter.is_symlink()
    assert squatter.read_bytes() == b"agent-owned"


@pytest.mark.parametrize("victim_kind", ["file", "symlink"])
def test_a_miss_never_follows_a_planted_directory_link(
    tmp_path: Path, http_server: _HttpServer, victim_kind: str
) -> None:
    base, _routes = http_server
    inputs_path = _write_inputs(
        tmp_path,
        {"m": {"value": [{"description": "x", "url": f"{base}/missing.png"}], "trusted": True}},
    )
    victim = _plant_outside_dir(tmp_path, victim_kind)

    assert main(["prefetch_script.py", str(inputs_path)]) == 0

    _assert_victim_untouched(victim, victim_kind)


def test_a_value_nested_too_deep_is_not_prefetched(
    tmp_path: Path, http_server: _HttpServer
) -> None:
    """宿主 ``linked_sites`` 对嵌套过深的值不给名字(渲染层就不会提它);沙箱这边也不能
    去拉、去建链接 —— 两边说的站点必须是同一组。"""
    base, routes = http_server
    served: list[str] = []
    routes["/deep.png"] = _route(b"\x89PNG" + b"H" * 16, content_type="image/png", served=served)
    deep: Any = {"description": "x", "url": f"{base}/deep.png"}
    for _ in range(40):
        deep = [deep]
    inputs_path = _write_inputs(tmp_path, {"deep": {"value": deep, "trusted": True}})

    assert main(["prefetch_script.py", str(inputs_path)]) == 0

    assert served == []
    assert not os.path.lexists(tmp_path / "inputs" / "run1" / "deep")


def test_a_manifest_nested_too_deep_to_load_still_exits_zero(tmp_path: Path) -> None:
    """清单是沙箱代码碰得到的文件;``json.load`` 在极深的嵌套上抛的是 ``RecursionError``
    (不是 ``ValueError``)—— 同样当「读不了」:以 0 退出、不改文件。"""
    run_dir = tmp_path / "inputs" / "run1"
    run_dir.mkdir(parents=True)
    path = run_dir / "inputs.json"
    text = "[" * 50_000 + "]" * 50_000
    path.write_text(text, encoding="utf-8")

    assert main(["prefetch_script.py", str(path)]) == 0
    assert path.read_text(encoding="utf-8") == text
