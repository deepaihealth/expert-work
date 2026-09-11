"""B-51 —— 平台 provider 依赖登记表 + 自审扫描。

登记表漏一行 = 凭据页漏一条警示 = 运维照旧把「平台在用」读成「没人用」。
所以这里不只测解析,更测**覆盖**:``settings.py`` 里每一个 ``Provider`` 型
或 ``*_provider`` 命名的平台配置都必须在表里出现。
"""

from __future__ import annotations

import ast
import inspect
import textwrap

from control_plane.platform_provider_uses import (
    PLATFORM_PROVIDER_USES,
    PlatformProviderUseOverride,
    resolve_platform_provider_uses,
)
from control_plane.settings import Settings


def _provider_settings_fields() -> set[str]:
    """``Settings`` 上所有 provider 型平台配置的字段名(AST 扫描)。

    判据两条(任一命中即算):注解是 ``Provider``,或字段名以 ``_provider``
    结尾 —— 后者兜住 ``rerank_provider`` 这类注解写成 ``str`` 的历史字段。
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(Settings)))
    class_def = tree.body[0]
    assert isinstance(class_def, ast.ClassDef)
    found: set[str] = set()
    for node in class_def.body:
        if not isinstance(node, ast.AnnAssign) or not isinstance(node.target, ast.Name):
            continue
        name = node.target.id
        if ast.unparse(node.annotation) == "Provider" or name.endswith("_provider"):
            found.add(name)
    return found


def test_registry_covers_every_provider_setting() -> None:
    """settings 里每个 provider 型配置都登记了 —— 少一个就红。"""
    registered = {spec.provider_setting for spec in PLATFORM_PROVIDER_USES}
    declared = _provider_settings_fields()
    assert declared, "AST 扫描一个 provider 字段都没找到 —— 扫描本身坏了"
    missing = declared - registered
    assert not missing, (
        f"这些平台 provider 配置没登记进 PLATFORM_PROVIDER_USES: {sorted(missing)} —— "
        "漏登记 = 凭据页不会为它警示"
    )


def test_registry_setting_names_all_exist_on_settings() -> None:
    """登记表写的是字段名,拼错了扫描也就白扫了。"""
    settings = Settings()
    for spec in PLATFORM_PROVIDER_USES:
        assert hasattr(settings, spec.provider_setting), spec.provider_setting
        assert hasattr(settings, spec.model_setting), spec.model_setting


def test_registry_features_are_unique() -> None:
    features = [spec.feature for spec in PLATFORM_PROVIDER_USES]
    assert len(features) == len(set(features))


def test_memory_consolidation_lands_on_the_default_aux_provider() -> None:
    """病理原点:anthropic 行必须挂着长期记忆整合这条依赖。"""
    settings = Settings(enable_scheduler=True)
    resolved = resolve_platform_provider_uses(settings)
    anthropic = {use.feature: use for use in resolved.get("anthropic", [])}
    assert "memory_consolidation" in anthropic
    assert anthropic["memory_consolidation"].enabled is True
    assert anthropic["memory_consolidation"].model == settings.memory_consolidator_default_aux_model


def test_scheduler_off_marks_memory_consolidation_disabled() -> None:
    resolved = resolve_platform_provider_uses(Settings(enable_scheduler=False))
    use = next(u for u in resolved["anthropic"] if u.feature == "memory_consolidation")
    assert use.enabled is False


def test_eval_worker_gate_drives_eval_agent_enabled() -> None:
    on = resolve_platform_provider_uses(Settings(enable_eval_worker=True))
    off = resolve_platform_provider_uses(Settings(enable_eval_worker=False))
    assert next(u for u in on["anthropic"] if u.feature == "eval_agent").enabled is True
    assert next(u for u in off["anthropic"] if u.feature == "eval_agent").enabled is False


def test_embedding_is_always_enabled() -> None:
    """向量化没有开关 —— 长期记忆写入与知识库入库都要它。"""
    resolved = resolve_platform_provider_uses(Settings(enable_scheduler=False))
    use = next(u for u in resolved["qwen"] if u.feature == "embedding")
    assert use.enabled is True


def test_override_wins_over_settings() -> None:
    """DB 覆盖层(质量裁判的 UI 开关默认关)必须压过 env 判据。"""
    settings = Settings(enable_quality_monitor=True)
    resolved = resolve_platform_provider_uses(
        settings,
        overrides={"quality_judge": PlatformProviderUseOverride(enabled=False)},
    )
    use = next(u for u in resolved["anthropic"] if u.feature == "quality_judge")
    assert use.enabled is False


def test_override_can_move_a_feature_to_another_provider() -> None:
    resolved = resolve_platform_provider_uses(
        Settings(),
        overrides={
            "embedding": PlatformProviderUseOverride(provider="openai", model="text-embedding-3")
        },
    )
    assert [u.feature for u in resolved["openai"]] == ["embedding"]
    assert all(u.feature != "embedding" for u in resolved.get("qwen", []))


def test_as_dict_shape_is_feature_model_enabled() -> None:
    resolved = resolve_platform_provider_uses(Settings())
    use = next(u for u in resolved["anthropic"] if u.feature == "memory_consolidation")
    assert set(use.as_dict()) == {"feature", "model", "enabled"}
