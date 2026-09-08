"""``sandbox_image_plan`` —— 集成测试 fixture「自己 build 还是借 CI 预构建的镜像」的判断。

两个 fixture(control-plane 的 egress e2e、sandbox-supervisor 的 acceptance)各自
``docker build`` 同一份 infra/sandbox-image;CI 在 pytest 之前用 gha cache 建好一份,
fixture 见到 ``EXPERT_WORK_TEST_SANDBOX_IMAGE`` 就 ``docker tag`` 借用。判断逻辑放在
一个纯函数里,这里把三种输入各钉一条:没设、设了、设了但是空白。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from expert_work.testing import PREBUILT_SANDBOX_IMAGE_ENV, sandbox_image_plan

_TAG = "expert-work-sandbox:itest"
_CONTEXT = Path("/repo/infra/sandbox-image")


def test_without_the_env_var_the_fixture_builds_the_image_itself() -> None:
    """本地开发没有这个变量 —— 行为必须与从前逐字相同。"""
    plan = sandbox_image_plan(_TAG, _CONTEXT, environ={})

    assert plan.argv == ("build", "-t", _TAG, str(_CONTEXT))
    assert plan.prebuilt is None


def test_with_the_env_var_the_fixture_tags_the_prebuilt_image_instead() -> None:
    plan = sandbox_image_plan(
        _TAG, _CONTEXT, environ={PREBUILT_SANDBOX_IMAGE_ENV: "expert-work-sandbox:ci"}
    )

    assert plan.argv == ("tag", "expert-work-sandbox:ci", _TAG)
    assert plan.prebuilt == "expert-work-sandbox:ci"


@pytest.mark.parametrize("blank", ["", "   "], ids=["empty", "whitespace"])
def test_a_blank_env_var_means_build_not_docker_tag_of_nothing(blank: str) -> None:
    """CI 里预构建步骤失败时把变量置空(``steps.x.outcome != success``)—— 那必须
    退回自己 build,而不是 ``docker tag "" ...``。"""
    plan = sandbox_image_plan(_TAG, _CONTEXT, environ={PREBUILT_SANDBOX_IMAGE_ENV: blank})

    assert plan.argv[0] == "build"
    assert plan.prebuilt is None


def test_the_default_environment_is_the_real_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """fixture 调用时不传 ``environ`` —— 默认读的必须是进程环境,否则 CI 设了也白设。"""
    monkeypatch.setenv(PREBUILT_SANDBOX_IMAGE_ENV, "expert-work-sandbox:ci")

    assert sandbox_image_plan(_TAG, _CONTEXT).prebuilt == "expert-work-sandbox:ci"
