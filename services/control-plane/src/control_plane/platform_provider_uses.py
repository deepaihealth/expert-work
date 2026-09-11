"""B-51 —— 平台自身对 provider 凭据的依赖登记表。

凭据页的 ``used_by_agents`` 只数 agent manifest 的引用;平台自己的后台功能
(向量化 / 重排 / 评测 agent / 质量裁判 / 长期记忆整合)同样要解平台凭据,
却一条都不计入。于是一行凭据可以同时显示「未设置」和「被智能体引用 0」,
运维据此判断不需要配 —— 完全合理,也完全错。长期记忆整合从上线起零成功
(``memory_consolidator.cluster_failed`` → ``platform credentials missing
for provider=anthropic``)就是这么来的。

这张表是那些依赖的**唯一登记处**:新增一个吃 provider 凭据的平台功能时,
在这里加一行。``tests/test_platform_provider_uses.py`` 的自审扫描盯着
``settings.py`` 里所有 ``Provider`` 型 / ``*_provider`` 命名的平台配置,
漏登记就红 —— 漏登记等于漏警示。

``feature`` 是稳定的机器标识,前端据此做 i18n;后端不返回人话文案。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from control_plane.settings import Settings


@dataclass(frozen=True)
class PlatformProviderUseSpec:
    """登记表的一行:一个平台功能对某个 provider 凭据的依赖。

    ``provider_setting`` / ``model_setting`` 是 :class:`Settings` 上的字段名
    (不是值)—— 自审扫描比对的就是这两个名字,所以必须写字段名而非硬编码
    provider id。``enabled`` 是 env 层的开关判据;真正生效还受 DB 覆盖层
    影响的功能由调用方通过 ``overrides`` 修正(见
    :func:`resolve_platform_provider_uses`)。
    """

    feature: str
    provider_setting: str
    model_setting: str
    enabled: Callable[[Settings], bool]


@dataclass(frozen=True)
class PlatformProviderUseOverride:
    """某个功能的运行期(DB 覆盖层)实况。``None`` = 该维度不覆盖。"""

    provider: str | None = None
    model: str | None = None
    enabled: bool | None = None


@dataclass(frozen=True)
class PlatformProviderUse:
    """解析后的一条平台依赖 —— 挂在对应 provider 的凭据行上。"""

    feature: str
    provider: str
    model: str
    enabled: bool

    def as_dict(self) -> dict[str, object]:
        """API 形态。``provider`` 不重复出现:它是所在行的行键。"""
        return {"feature": self.feature, "model": self.model, "enabled": self.enabled}


#: 平台级 provider 依赖登记表。**加平台功能就加这里一行。**
PLATFORM_PROVIDER_USES: tuple[PlatformProviderUseSpec, ...] = (
    PlatformProviderUseSpec(
        feature="embedding",
        provider_setting="embedding_provider",
        model_setting="embedding_model",
        # 向量化没有开关:长期记忆写入与知识库入库都要它,常开。
        enabled=lambda _settings: True,
    ),
    PlatformProviderUseSpec(
        feature="rerank",
        provider_setting="rerank_provider",
        model_setting="rerank_model",
        # 重排是可选增强(Mini-ADR O-9:缺凭据优雅降级成 RRF 融合序)。
        # env 层判据与 ``PlatformEmbeddingConfigService._env_pair`` 同义。
        enabled=lambda settings: settings.rerank_provider in settings.effective_supported_providers,
    ),
    PlatformProviderUseSpec(
        feature="eval_agent",
        provider_setting="eval_agent_provider",
        model_setting="eval_agent_model",
        enabled=lambda settings: settings.enable_eval_worker,
    ),
    PlatformProviderUseSpec(
        feature="quality_judge",
        provider_setting="quality_judge_provider",
        model_setting="quality_judge_model",
        # 部署级硬闸而已:真正生效是 ``enable_quality_monitor AND UI 开关``
        # (``platform_quality_config.enabled``,默认关),由 overrides 修正。
        enabled=lambda settings: settings.enable_quality_monitor,
    ),
    PlatformProviderUseSpec(
        feature="memory_consolidation",
        provider_setting="memory_consolidator_default_aux_provider",
        model_setting="memory_consolidator_default_aux_model",
        enabled=lambda settings: settings.enable_scheduler,
    ),
)


def resolve_platform_provider_uses(
    settings: Settings,
    *,
    overrides: Mapping[str, PlatformProviderUseOverride] | None = None,
) -> dict[str, list[PlatformProviderUse]]:
    """provider id → 该 provider 上的平台依赖列表(登记表 + DB 覆盖层)。"""
    resolved: dict[str, list[PlatformProviderUse]] = {}
    for spec in PLATFORM_PROVIDER_USES:
        override = (overrides or {}).get(spec.feature) or PlatformProviderUseOverride()
        provider = override.provider or str(getattr(settings, spec.provider_setting))
        model = override.model or str(getattr(settings, spec.model_setting))
        enabled = override.enabled if override.enabled is not None else spec.enabled(settings)
        resolved.setdefault(provider, []).append(
            PlatformProviderUse(
                feature=spec.feature, provider=provider, model=model, enabled=enabled
            )
        )
    return resolved


__all__ = [
    "PLATFORM_PROVIDER_USES",
    "PlatformProviderUse",
    "PlatformProviderUseOverride",
    "PlatformProviderUseSpec",
    "resolve_platform_provider_uses",
]
