"""B-112 —— every model a manifest names, and the save-time catalog check.

A misspelled model name used to pass save / publish: the vendor answers
"model not found" with a 4xx, which by design does not fall back, so the
fallback chain cannot protect against the most common configuration mistake
and the first run is where it surfaces. The built-in providers are called at
their fixed official endpoints with the platform key, and the platform can
only price and capability-gate what is in the model catalog — so for them a
name outside the catalog is either a typo or an unmetered call. Both are
refused when the manifest is saved.

``azure`` / ``self-hosted`` are exempt: their model names are the tenant's own
(an Azure deployment, a self-hosted server's served name) and the catalog has
no entries for them by design. Deprecated catalog entries pass — retiring a
model takes it off the dropdown, it must not make agents already using it
unsaveable.

:func:`iter_manifest_models` is the one walk over the manifest's model fields;
the provider whitelist gate and the credentials panel use it too, so a new
model field cannot be checked by one and missed by the others.
"""

from __future__ import annotations

from collections.abc import Iterator

from expert_work.protocol import AgentSpec, ModelSpec
from expert_work.protocol.model_catalog import catalog_entry

#: Providers whose model names the tenant chooses — nothing to check them against.
UNCATALOGUED_PROVIDERS = frozenset({"azure", "self-hosted"})


def iter_manifest_models(spec: AgentSpec) -> Iterator[tuple[str, ModelSpec]]:
    """Yield ``(field path, model)`` for every model the manifest names,
    fallbacks included, in document order."""
    body = spec.spec
    yield from _with_fallbacks("spec.model", body.model)
    if body.routing is not None:
        for index, rule in enumerate(body.routing.rules):
            yield from _with_fallbacks(f"spec.routing.rules[{index}].model", rule.model)
    if body.dynamic_workers.model is not None:
        yield from _with_fallbacks("spec.dynamic_workers.model", body.dynamic_workers.model)
    if body.vision is not None:
        yield from _with_fallbacks("spec.vision.model", body.vision.model)
        for index, model in enumerate(body.vision.fallbacks):
            yield from _with_fallbacks(f"spec.vision.fallbacks[{index}]", model)
    aux_model = body.policies.memory_consolidation.aux_model
    if aux_model is not None:
        yield from _with_fallbacks("spec.policies.memory_consolidation.aux_model", aux_model)


def _with_fallbacks(path: str, model: ModelSpec) -> Iterator[tuple[str, ModelSpec]]:
    yield path, model
    for index, fallback in enumerate(model.fallback):
        yield from _with_fallbacks(f"{path}.fallback[{index}]", fallback)


def off_catalog_models_error(spec: AgentSpec) -> str | None:
    """The refusal text naming every off-catalog model (field path, value,
    provider), or ``None`` when every name checks out."""
    offending = [
        f"{path}.name = {model.name!r} ({model.provider})"
        for path, model in iter_manifest_models(spec)
        if model.provider not in UNCATALOGUED_PROVIDERS
        and catalog_entry(model.provider, model.name) is None
    ]
    if not offending:
        return None
    return (
        "模型名不在该厂商的模型目录里, 请检查拼写或从模型列表里选 / "
        "model name not in the provider's model catalog - check the spelling "
        "or pick one from the model list: " + "; ".join(offending)
    )
