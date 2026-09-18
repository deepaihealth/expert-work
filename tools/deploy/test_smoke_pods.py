"""``smoke.sh`` 的 pod 就绪判据 —— 排空中的旧 pod 不算发布失败。

2026-09-18 实况:一条对话正在跑,滚动发布把旧 control-plane pod 置成
Terminating,它按 B-80 一直等那个 run 收尾;smoke 把它报成 FAIL,``release.sh``
于是停在阶段 5,**阶段 6 的金丝雀没跑** —— 那是唯一真起 run、写文件、下载产物
的检查。判据必须分清「还在正常收尾」和「卡住了」。

三种形态各一条,少一条这组测试就不成立:

* Terminating 且仍在自己的宽限期内 → 放行(这是健康发布的常态)
* Terminating 且已超出宽限期 → **报** (kubelet 早该 SIGKILL 了,是真异常)
* 真的坏了(CrashLoopBackOff 等) → **报**(别为了放行排空把这类一起放了)

``smoke.sh`` 整体要连集群,所以这里只把待测的那几个函数抽出来在 bash 里跑,
kubectl 用 stub。
"""

from __future__ import annotations

import os
import re
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

_SMOKE_SH = Path(__file__).resolve().parent / "smoke.sh"
_GRACE_S = 360  # 与 deployment.yaml 的 terminationGracePeriodSeconds 同值


def _stamp(*, seconds_ago: int) -> str:
    moment = datetime.now(UTC) - timedelta(seconds=seconds_ago)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _functions_under_test() -> str:
    """抽出 ``utc_minus`` / ``draining_within_grace`` / ``pods_not_ready`` 三段。

    照抄一份会让测试给它自己的副本发合格证 —— 必须从真文件里取。
    """
    text = _SMOKE_SH.read_text(encoding="utf-8")
    out = []
    for name in ("utc_minus", "draining_within_grace", "pods_not_ready"):
        match = re.search(rf"^{name}\(\) \{{$.*?^\}}$", text, re.MULTILINE | re.DOTALL)
        assert match is not None, f"smoke.sh 里找不到函数 {name}"
        out.append(match.group(0))
    slack = re.search(r"^GRACE_SLACK_S=\d+", text, re.MULTILINE)
    assert slack is not None, "smoke.sh 里找不到 GRACE_SLACK_S"
    return slack.group(0) + "\n" + "\n".join(out)


def _run(tmp_path: Path, *, wide: str, jsonpath: str) -> str:
    """跑 ``pods_not_ready``,kubectl 由 stub 顶替。"""
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    stub = stub_dir / "kubectl"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        'case " $* " in\n'
        '    *"--no-headers"*) cat "${STUB_WIDE}" ;;\n'
        '    *"jsonpath="*) cat "${STUB_JSONPATH}" ;;\n'
        "esac\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    (tmp_path / "wide").write_text(wide, encoding="utf-8")
    (tmp_path / "jsonpath").write_text(jsonpath, encoding="utf-8")

    env = {
        **os.environ,
        "PATH": f"{stub_dir}:{os.environ['PATH']}",
        "STUB_WIDE": str(tmp_path / "wide"),
        "STUB_JSONPATH": str(tmp_path / "jsonpath"),
        "LC_ALL": "C",
    }
    script = f"set -euo pipefail\n{_functions_under_test()}\npods_not_ready\n"
    done = subprocess.run(  # noqa: S603 — fixed argv, no shell, test harness
        ["bash", "-c", script],  # noqa: S607 — bash from PATH is the point (stubbed kubectl)
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


_OLD = "control-plane-85769dc59b-bl4v9"
_NEW = "control-plane-69f7b5fb7c-bqc8b"


def _wide(old_status: str) -> str:
    return (
        f"{_NEW}   1/1   Running   0   8m\n"
        f"{_OLD}   0/1   {old_status}   0   5h39m\n"
        "migrate-l5x72   0/1   Completed   0   9m\n"
    )


def test_draining_pod_inside_its_grace_is_not_a_failure(tmp_path: Path) -> None:
    jsonpath = f"{_NEW}||{_GRACE_S}\n{_OLD}|{_stamp(seconds_ago=120)}|{_GRACE_S}\n"
    assert _run(tmp_path, wide=_wide("Terminating"), jsonpath=jsonpath) == "none"


def test_draining_pod_past_its_grace_is_reported(tmp_path: Path) -> None:
    """超出宽限期还在 —— kubelet 早该硬杀了,这才是真异常。"""
    stale = _stamp(seconds_ago=_GRACE_S + 600)
    jsonpath = f"{_NEW}||{_GRACE_S}\n{_OLD}|{stale}|{_GRACE_S}\n"
    assert _run(tmp_path, wide=_wide("Terminating"), jsonpath=jsonpath) == f"{_OLD}(Terminating)"


def test_a_genuinely_broken_pod_is_still_reported(tmp_path: Path) -> None:
    """放行排空不能顺手把真故障也放了 —— 这条是那个改法的分水岭。"""
    jsonpath = f"{_NEW}||{_GRACE_S}\n{_OLD}|{_stamp(seconds_ago=120)}|{_GRACE_S}\n"
    wide = f"{_NEW}   0/1   CrashLoopBackOff   7   8m\n{_OLD}   0/1   Terminating   0   5h39m\n"
    assert _run(tmp_path, wide=wide, jsonpath=jsonpath) == f"{_NEW}(CrashLoopBackOff)"


@pytest.mark.parametrize("grace", ["", "30"])
def test_missing_or_short_grace_still_parses(tmp_path: Path, grace: str) -> None:
    """``terminationGracePeriodSeconds`` 缺省时不能炸,按 k8s 默认 30s 算。"""
    jsonpath = f"{_NEW}||{_GRACE_S}\n{_OLD}|{_stamp(seconds_ago=5)}|{grace}\n"
    assert _run(tmp_path, wide=_wide("Terminating"), jsonpath=jsonpath) == "none"
