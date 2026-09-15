"""B-61 Task 2 —— 预拉脚本的判定逻辑(不起沙箱,直接 import)。"""

from __future__ import annotations

import ast
import http.server
import json
import socket
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, ClassVar

import pytest

from orchestrator.tools import prefetch_script
from orchestrator.tools.prefetch_script import (
    MAX_FILE_BYTES,
    MAX_TOTAL_BYTES,
    content_type_ok,
    main,
    pick_suffix,
    script_source,
    target_name,
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


def test_target_name_encodes_the_site_so_two_urls_never_collide() -> None:
    assert target_name("org_logo", [], ".jpg") == "org_logo.jpg"
    assert target_name("materials", [0, "url"], ".mp4") == "materials.0.url.mp4"


def test_target_name_rejects_traversal_in_the_variable_name() -> None:
    with pytest.raises(ValueError, match="unsafe"):
        target_name("../../etc/passwd", [], ".jpg")


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
    from orchestrator.tools.inputs_doc import iter_url_sites
    from orchestrator.tools.prefetch_script import _sites

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
        }
    }
    host = [(s.var_name, list(s.path), s.url) for s in iter_url_sites(doc)]
    sandbox = [
        (name, path, url)
        for name, entry in doc["variables"].items()
        for path, url in _sites(entry["value"], [])
    ]
    assert host == sandbox
    # 显式钉住攻击场景本身:c 只应该命中 url 那条,local_path 绝不能被当成待预拉的地址。
    assert ("c", ["url"], "https://ok/a.mp4") in sandbox
    assert not any(name == "c" and url == "https://attacker/b.mp4" for name, _, url in sandbox)


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
    assert doc["variables"]["org_logo"]["local_path"] == "inputs/run1/files/org_logo.jpg"
    saved = tmp_path / "inputs" / "run1" / "files" / "org_logo.jpg"
    assert saved.read_bytes() == body


def test_fetch_404_is_a_miss(tmp_path: Path, http_server: _HttpServer) -> None:
    base, _routes = http_server
    inputs_path = _write_inputs(
        tmp_path, {"org_logo": {"value": f"{base}/missing.jpg", "trusted": True}}
    )

    assert main(["prefetch_script.py", str(inputs_path)]) == 0

    doc = json.loads(inputs_path.read_text(encoding="utf-8"))
    assert doc["variables"]["org_logo"]["local_path"] is None
    assert not (tmp_path / "inputs" / "run1" / "files").exists()


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
    assert not (tmp_path / "inputs" / "run1" / "files").exists()


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
    assert not (tmp_path / "inputs" / "run1" / "files").exists()


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
    assert not (tmp_path / "inputs" / "run1" / "files").exists()


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
    assert doc["variables"]["a"]["local_path"] == "inputs/run1/files/a.jpg"
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
