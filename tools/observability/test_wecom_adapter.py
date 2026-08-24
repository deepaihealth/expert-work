"""wecom-adapter(infra/k8s/base/observability/wecom-adapter/adapter.py)纯函数单测。

adapter 以 ConfigMap 脚本形态部署,不属于任何包 —— 按路径加载模块。
"""

from __future__ import annotations

import importlib.util
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
