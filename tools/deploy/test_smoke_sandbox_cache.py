"""``smoke.sh`` 的沙箱镜像缓存判据(2026-10-16)。

冷启动一台沙箱约 66 s,42 s 在拉沙箱镜像;开了 ACS 镜像缓存后测试集群 11 s。
缓存要组件、权限、池模板注解三样都在,任何一样丢了冷启动就悄悄变回一分多钟,
所以 smoke 要说一句。判据只认「待命沙箱命中了**钉住的那个镜像**的缓存」:同一个
Pod 上挂盘组件的缓存也会命中,那不算;旧 tag 的缓存也不算。

函数是纯的(输入全是 kubectl 的输出),从真文件里抽出来直接驱动。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

_SMOKE_SH = Path(__file__).resolve().parent / "smoke.sh"
_IMAGE = "crpi-x-vpc.cn-hangzhou.personal.cr.aliyuncs.com/expert-work/sandbox:new1234"
_OLD_IMAGE = "crpi-x-vpc.cn-hangzhou.personal.cr.aliyuncs.com/expert-work/sandbox:old5678"
_CSI = "registry-cn-hangzhou-vpc.ack.aliyuncs.com/acs/csi-agent:v1"


def _verdict(*, enabled: str, caches: list[str], matched: list[str], image: str = _IMAGE) -> str:
    text = _SMOKE_SH.read_text(encoding="utf-8")
    match = re.search(r"^sandbox_cache_verdict\(\) \{$.*?^\}$", text, re.MULTILINE | re.DOTALL)
    assert match is not None, "smoke.sh 里找不到函数 sandbox_cache_verdict"
    script = f'set -euo pipefail\n{match.group(0)}\nsandbox_cache_verdict "$1" "$2" "$3" "$4"\n'
    done = subprocess.run(  # noqa: S603 — fixed argv, no shell, test harness
        ["bash", "-c", script, "verdict", enabled, "\n".join(caches), "\n".join(matched), image],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout


def _matched(*ids: str) -> str:
    return "[" + ",".join(f'{{"imageCacheId":"{i}","size":1}}' for i in ids) + "]"


def test_hit_on_the_pinned_image_is_ok() -> None:
    out = _verdict(
        enabled="true",
        caches=[f"{_CSI}|imc-csi|Ready", f"{_IMAGE}|imc-main|Ready"],
        matched=[_matched("imc-csi", "imc-main")],
    )
    assert out.startswith("OK   sandbox image cache hit (sandbox:new1234, imc-main)")


def test_pool_without_the_annotation_warns() -> None:
    out = _verdict(enabled="", caches=[f"{_IMAGE}|imc-main|Ready"], matched=[_matched("imc-main")])
    assert out.startswith("WARN") and "enable-image-cache" in out


def test_only_a_sidecar_cache_matched_is_not_a_hit() -> None:
    """挂盘组件的缓存命中了,沙箱镜像没命中:冷启动还是要拉 42 s。"""
    out = _verdict(
        enabled="true",
        caches=[f"{_CSI}|imc-csi|Ready", f"{_IMAGE}|imc-main|Ready"],
        matched=[_matched("imc-csi")],
    )
    assert out.startswith("WARN") and "imc-main" in out and "没命中" in out


def test_a_cache_of_the_previous_tag_does_not_count() -> None:
    """刚换镜像:旧 tag 有缓存、新 tag 还没有 —— 待命沙箱即使命中旧缓存也不算。"""
    out = _verdict(
        enabled="true",
        caches=[f"{_OLD_IMAGE}|imc-old|Ready"],
        matched=[_matched("imc-old")],
    )
    assert out.startswith("WARN") and "还没有镜像缓存" in out


def test_a_failed_record_before_the_good_one_is_skipped() -> None:
    """同一镜像先有一条失败、没有 id 的记录(10-16 测试集群实况),再有一条做好的。"""
    out = _verdict(
        enabled="true",
        caches=[f"{_IMAGE}||Failed", f"{_IMAGE}|imc-main|Ready"],
        matched=[_matched("imc-main")],
    )
    assert out.startswith("OK")


def test_no_cache_record_at_all_warns() -> None:
    """组件没装(没有 ImageCache 这个资源)时 kubectl 输出为空。"""
    out = _verdict(enabled="true", caches=[], matched=[""])
    assert out.startswith("WARN") and "还没有镜像缓存" in out
