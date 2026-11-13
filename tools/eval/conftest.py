"""Put this directory on ``sys.path`` so the tests can ``import expert_work_eval``.

``tools/eval`` is a dev tool, not an installed workspace package — the
harness module lives next to its tests.
"""

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))


@pytest.fixture(autouse=True)
def _b300_default_state_dir(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[None]:
    """B-300 的花费总账永远也读、也锁默认状态目录(``~/.cache/b300-eval``);测试里把它换成临时
    目录,不碰本机真实的账本。"""
    # 按文件名认(模块名可能带包前缀,如 ``tools.eval.test_b300_spend``)。
    if not Path(str(request.module.__file__)).name.startswith("test_b300"):
        yield
        return
    import b300_spend

    monkeypatch = pytest.MonkeyPatch()
    fake_default = tmp_path_factory.mktemp("b300-default")
    monkeypatch.setattr(b300_spend, "DEFAULT_STATE_DIR", fake_default)
    assert b300_spend.DEFAULT_STATE_DIR == fake_default  # 绝不碰本机真实的账本目录
    try:
        yield
    finally:
        monkeypatch.undo()
