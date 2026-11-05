"""``smoke.sh`` 的建沙箱失败占比(B-87 ①)。

2026-09-19 才发现 ``destroy_reason='create_failed'`` 占 sandbox_instance 的 18.8%、当天 87%,
而发布闸门从来不看它。smoke 每次发布报一次最近 24h 的占比,只 WARN 不 FAIL(趋势不是故障,
不该叫人回滚一个健康的发布)。

函数是纯的(输入是 pod 里查出来的两个数),从真文件里抽出来直接驱动;``check=True`` 同时钉住
「任何输入都不让 smoke 以非零退出」。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

_SMOKE_SH = Path(__file__).resolve().parent / "smoke.sh"


def _verdict(failed: str, total: str) -> str:
    text = _SMOKE_SH.read_text(encoding="utf-8")
    match = re.search(
        r"^sandbox_create_failed_verdict\(\) \{$.*?^\}$", text, re.MULTILINE | re.DOTALL
    )
    assert match is not None, "smoke.sh 里找不到函数 sandbox_create_failed_verdict"
    script = f'set -euo pipefail\n{match.group(0)}\nsandbox_create_failed_verdict "$1" "$2"\n'
    done = subprocess.run(  # noqa: S603 — fixed argv, no shell, test harness
        ["bash", "-c", script, "verdict", failed, total],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout


def test_no_failures_is_ok() -> None:
    assert _verdict("0", "42").startswith("OK   sandbox create failures (24h): 0/42 (0.0%)")


def test_the_b87_day_warns_with_the_share() -> None:
    """09-19:15 台里 13 台没建起来。"""
    out = _verdict("13", "15")
    assert out.startswith("WARN sandbox create failures (24h): 13/15 (86.7%)")
    assert "sandbox create failed" in out


def test_one_failure_on_a_quiet_day_is_enough_to_warn() -> None:
    assert _verdict("1", "15").startswith("WARN")


def test_a_rare_failure_on_a_busy_day_stays_ok() -> None:
    assert _verdict("1", "100").startswith("OK   sandbox create failures (24h): 1/100 (1.0%)")


def test_no_sandboxes_created_is_ok() -> None:
    assert _verdict("0", "0").startswith("OK")


def test_no_query_result_warns_instead_of_failing() -> None:
    """kubectl exec 失败 / 表结构变了:查不出数也只 WARN,smoke 不能因此红。"""
    assert _verdict("", "").startswith("WARN sandbox create failures: ")
    assert _verdict("Traceback", "").startswith("WARN sandbox create failures: ")
