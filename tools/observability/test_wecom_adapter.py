"""wecom-adapter(infra/k8s/base/observability/wecom-adapter/adapter.py)纯函数单测。

adapter 以 ConfigMap 脚本形态部署,不属于任何包 —— 按路径加载模块。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_ADAPTER = (
    Path(__file__).resolve().parents[2]
    / "infra"
    / "k8s"
    / "base"
    / "observability"
    / "wecom-adapter"
    / "adapter.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("wecom_adapter", _ADAPTER)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


adapter = _load()


def _payload(*alerts: dict) -> dict:
    return {"status": "firing", "alerts": list(alerts)}


def _alert(
    status: str = "firing",
    name: str = "HighErrorRate",
    summary: str = "5xx over 5%",
    starts: str = "2026-08-24T09:00:00Z",
) -> dict:
    return {
        "status": status,
        "labels": {"alertname": name, "severity": "P0"},
        "annotations": {"summary": summary},
        "startsAt": starts,
    }


class TestBuildMessages:
    def test_firing_alert_renders_markdown_with_name_and_summary(self) -> None:
        msgs = adapter.build_messages("p1", _payload(_alert()))
        assert msgs[0]["msgtype"] == "markdown"
        content = msgs[0]["markdown"]["content"]
        assert "P1" in content
        assert "HighErrorRate" in content
        assert "5xx over 5%" in content
        assert "FIRING" in content

    def test_resolved_alert_marked_resolved(self) -> None:
        msgs = adapter.build_messages("p2", _payload(_alert(status="resolved")))
        content = msgs[0]["markdown"]["content"]
        assert "RESOLVED" in content
        assert "FIRING" not in content

    def test_p0_firing_appends_text_mention_all(self) -> None:
        msgs = adapter.build_messages("p0", _payload(_alert()))
        assert len(msgs) == 2
        assert msgs[1]["msgtype"] == "text"
        assert msgs[1]["text"]["mentioned_list"] == ["@all"]

    def test_p0_all_resolved_no_mention(self) -> None:
        msgs = adapter.build_messages("p0", _payload(_alert(status="resolved")))
        assert len(msgs) == 1

    def test_non_p0_never_mentions(self) -> None:
        for channel in ("p1", "p2", "default"):
            msgs = adapter.build_messages(channel, _payload(_alert()))
            assert len(msgs) == 1, channel

    def test_empty_alerts_returns_no_messages(self) -> None:
        assert adapter.build_messages("p2", {"status": "firing", "alerts": []}) == []

    def test_missing_fields_do_not_crash(self) -> None:
        msgs = adapter.build_messages("p2", _payload({}))
        assert msgs and "unknown" in msgs[0]["markdown"]["content"]

    def test_long_content_truncated_under_wecom_byte_cap(self) -> None:
        alerts = [_alert(summary="长" * 500, name=f"A{i}") for i in range(30)]
        msgs = adapter.build_messages("p2", _payload(*alerts))
        content = msgs[0]["markdown"]["content"]
        assert len(content.encode("utf-8")) <= adapter.MARKDOWN_BYTE_CAP
        assert content.endswith("…")

    def test_flood_folds_beyond_max_rendered(self) -> None:
        """25 条【短】告警:确保走的是 MAX_ALERTS_RENDERED 防洪泛,而非字节截断
        (终审 I-2:此前只有长内容用例,折叠逻辑死活测试看不出来)。"""
        alerts = [_alert(summary=f"s{i}", name=f"A{i}") for i in range(25)]
        msgs = adapter.build_messages("p2", _payload(*alerts))
        content = msgs[0]["markdown"]["content"]
        assert "另 5 条未展开" in content
        rendered = [ln for ln in content.splitlines() if ln.startswith("> **")]
        assert len(rendered) == adapter.MAX_ALERTS_RENDERED
        assert not content.endswith("…")  # 未触发字节截断,折叠是自己的功劳

    def test_runbook_url_rendered(self) -> None:
        a = _alert()
        a["annotations"]["runbook_url"] = "https://runbooks.example/high-error"
        msgs = adapter.build_messages("p1", _payload(a))
        assert "https://runbooks.example/high-error" in msgs[0]["markdown"]["content"]

    def test_markdown_is_always_first_message(self) -> None:
        """handler 的 log-only/首条判定依赖「markdown 恒为第一条」。"""
        msgs = adapter.build_messages("p0", _payload(_alert()))
        assert msgs[0]["msgtype"] == "markdown"


class TestPostWecom:
    def test_rejects_non_https(self) -> None:
        import pytest

        with pytest.raises(ValueError):
            adapter._post_wecom("http://qyapi.weixin.qq.com/x", {"msgtype": "text"})

    def test_raises_typed_error_on_errcode(self, monkeypatch) -> None:
        import io
        import pytest

        class _Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        monkeypatch.setattr(
            adapter.urllib.request,
            "urlopen",
            lambda *a, **k: _Resp(b'{"errcode": 93000, "errmsg": "invalid key"}'),
        )
        with pytest.raises(adapter.WecomDeliveryError) as ei:
            adapter._post_wecom("https://qyapi.weixin.qq.com/x", {"msgtype": "text"})
        assert ei.value.errcode == 93000

    def test_ok_on_errcode_zero(self, monkeypatch) -> None:
        import io

        class _Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        monkeypatch.setattr(
            adapter.urllib.request,
            "urlopen",
            lambda *a, **k: _Resp(b'{"errcode": 0, "errmsg": "ok"}'),
        )
        adapter._post_wecom("https://qyapi.weixin.qq.com/x", {"msgtype": "text"})


class TestHandler:
    """真 ThreadingHTTPServer 上跑 handler 全路径(终审 I-3)。"""

    @staticmethod
    def _serve():
        import threading

        server = adapter.ThreadingHTTPServer(("127.0.0.1", 0), adapter._Handler)
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        return server, f"http://127.0.0.1:{server.server_address[1]}"

    @staticmethod
    def _post(base: str, path: str, body: bytes) -> int:
        import urllib.error
        import urllib.request

        try:
            return urllib.request.urlopen(
                urllib.request.Request(base + path, data=body, method="POST"), timeout=5
            ).status
        except urllib.error.HTTPError as e:
            return e.code

    def test_healthz_and_404(self) -> None:
        import urllib.error
        import urllib.request

        server, base = self._serve()
        try:
            assert urllib.request.urlopen(base + "/healthz", timeout=5).status == 200
            try:
                urllib.request.urlopen(base + "/nope", timeout=5)
                raise AssertionError("expected 404")
            except urllib.error.HTTPError as e:
                assert e.code == 404
        finally:
            server.shutdown()

    def test_bad_payload_400_and_log_only_200(self, monkeypatch) -> None:
        monkeypatch.delenv("WECOM_WEBHOOK_URL", raising=False)
        server, base = self._serve()
        try:
            assert self._post(base, "/alert?channel=p2", b"not json") == 400
            good = json.dumps(_payload(_alert())).encode()
            assert self._post(base, "/alert?channel=p2", good) == 200  # log-only
        finally:
            server.shutdown()

    def test_delivery_failure_502_and_rate_limit_200(self, monkeypatch) -> None:
        monkeypatch.setenv("WECOM_WEBHOOK_URL", "https://qyapi.weixin.qq.com/x")
        good = json.dumps(_payload(_alert())).encode()
        server, base = self._serve()
        try:

            def _boom(url, message):
                raise adapter.WecomDeliveryError(93000, "invalid key")

            monkeypatch.setattr(adapter, "_post_wecom", _boom)
            assert self._post(base, "/alert?channel=p2", good) == 502

            def _limited(url, message):
                raise adapter.WecomDeliveryError(adapter.WECOM_RATE_LIMITED, "freq limit")

            monkeypatch.setattr(adapter, "_post_wecom", _limited)
            assert self._post(base, "/alert?channel=p2", good) == 200
        finally:
            server.shutdown()

    def test_p0_followup_failure_still_200(self, monkeypatch) -> None:
        """markdown 成功、@all 失败 → 200(不重投防主消息翻倍,终审 M-6)。"""
        monkeypatch.setenv("WECOM_WEBHOOK_URL", "https://qyapi.weixin.qq.com/x")
        calls: list[str] = []

        def _second_fails(url, message):
            calls.append(message["msgtype"])
            if len(calls) > 1:
                raise adapter.WecomDeliveryError(93000, "boom")

        monkeypatch.setattr(adapter, "_post_wecom", _second_fails)
        server, base = self._serve()
        try:
            good = json.dumps(_payload(_alert())).encode()
            assert self._post(base, "/alert?channel=p0", good) == 200
            assert calls == ["markdown", "text"]
        finally:
            server.shutdown()
