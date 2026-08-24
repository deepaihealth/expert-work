"""Alertmanager → 企业微信群机器人 relay(PROD-2)。

Alertmanager 的 webhook JSON 企微群机器人读不懂,且 Alertmanager 配置不展开
env var、企微 webhook URL(含 key)不能进 git —— 所以中间垫这个 stdlib-only
的小转换层:

    alertmanager receivers → POST http://wecom-adapter:8080/alert?channel=p0|p1|p2
    adapter → POST $WECOM_WEBHOOK_URL(k8s Secret ``wecom-alert-webhook``)

行为:
- 每个 channel 一条 markdown 消息(FIRING/RESOLVED 逐条列出);
- p0 且含 firing 时额外发一条 text + @all(企微 markdown 不支持 @);
- ``WECOM_WEBHOOK_URL`` 未配置 → log-only 降级返 200(集群不红,便于
  企微群建好前先部署;secrets.example.yaml 记了开通步骤);
- 企微侧失败 → 502,让 Alertmanager 按自身重试语义重投。

部署形态:ConfigMap 脚本 + control-plane 镜像(集群 VPC 够不着 docker.io,
复用已镜像的 python 运行时,零新镜像)。改这个文件就是改 ConfigMap,随
`kubectl apply -k` 滚动。单测:tools/observability/test_wecom_adapter.py。
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("wecom-adapter")

#: 企微 markdown content 上限 4096 字节,留头部余量。
MARKDOWN_BYTE_CAP = 4000
#: 单条消息最多渲染的告警数(超出计数折叠,防洪泛)。
MAX_ALERTS_RENDERED = 20

_CHANNEL_ICON = {"p0": "🔴", "p1": "🟠", "p2": "🟡"}


def _truncate_utf8(text: str, cap: int) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= cap:
        return text
    return raw[: cap - len("…".encode("utf-8"))].decode("utf-8", errors="ignore") + "…"


def build_messages(channel: str, payload: dict) -> list[dict]:
    """Alertmanager webhook payload → 企微机器人消息列表(纯函数)。"""
    alerts = payload.get("alerts") or []
    if not alerts:
        return []

    icon = _CHANNEL_ICON.get(channel, "⚪")
    lines = [f"{icon} **[{channel.upper()}] 告警通知**"]
    firing = 0
    for alert in alerts[:MAX_ALERTS_RENDERED]:
        labels = alert.get("labels") or {}
        annotations = alert.get("annotations") or {}
        name = labels.get("alertname", "unknown")
        summary = annotations.get("summary") or annotations.get("description") or ""
        if alert.get("status") == "resolved":
            status = "✅ RESOLVED"
        else:
            status = "🔥 FIRING"
            firing += 1
        line = f"> **{status}** {name}"
        if summary:
            line += f":{summary}"
        starts = alert.get("startsAt")
        if starts:
            line += f"(since {starts})"
        lines.append(line)
    if len(alerts) > MAX_ALERTS_RENDERED:
        lines.append(f"> …另 {len(alerts) - MAX_ALERTS_RENDERED} 条未展开")

    content = _truncate_utf8("\n".join(lines), MARKDOWN_BYTE_CAP)
    messages: list[dict] = [{"msgtype": "markdown", "markdown": {"content": content}}]
    if channel == "p0" and firing:
        messages.append(
            {
                "msgtype": "text",
                "text": {
                    "content": f"P0 告警 {firing} 条,请立即处理(5 分钟 SLA)",
                    "mentioned_list": ["@all"],
                },
            }
        )
    return messages


def _post_wecom(url: str, message: dict) -> None:
    """单条投递;企微侧 HTTP 非 200 或 errcode != 0 时抛异常。"""
    request = urllib.request.Request(
        url,
        data=json.dumps(message, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        body = json.loads(response.read().decode("utf-8"))
    if body.get("errcode") != 0:
        raise RuntimeError(f"wecom errcode={body.get('errcode')} errmsg={body.get('errmsg')}")


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: object) -> None:  # 静默默认访问日志
        pass

    def _reply(self, code: int, body: bytes = b"") -> None:
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if urlparse(self.path).path == "/healthz":
            self._reply(200, b"ok")
        else:
            self._reply(404)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/alert":
            self._reply(404)
            return
        channel = (parse_qs(parsed.query).get("channel") or ["default"])[0]
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._reply(400, b"bad payload")
            return

        messages = build_messages(channel, payload)
        if not messages:
            self._reply(200, b"no alerts")
            return

        url = os.environ.get("WECOM_WEBHOOK_URL", "").strip()
        if not url:
            log.warning(
                "WECOM_WEBHOOK_URL unset — log-only mode, dropping %d message(s) for %s: %s",
                len(messages),
                channel,
                messages[0]["markdown"]["content"][:200],
            )
            self._reply(200, b"log-only")
            return

        try:
            for message in messages:
                _post_wecom(url, message)
        except Exception:
            log.exception("wecom delivery failed for channel=%s", channel)
            self._reply(502, b"wecom delivery failed")
            return
        self._reply(200, b"delivered")


def main() -> None:
    port = int(os.environ.get("PORT", "8080"))
    if not os.environ.get("WECOM_WEBHOOK_URL", "").strip():
        log.warning("WECOM_WEBHOOK_URL unset — starting in log-only mode")
    server = ThreadingHTTPServer(("0.0.0.0", port), _Handler)
    log.info("wecom-adapter listening on :%d", port)
    server.serve_forever()


if __name__ == "__main__":
    main()
