/**
 * Process-lifetime cache + pure lookups over the model catalog. Mirrors
 * ``schema.ts``. Lookups are plain functions so they're trivially testable.
 */
import {
  fetchModelCatalog,
  type CatalogModel,
  type ModelCatalog,
} from "../../api/model_catalog";

let cached: Promise<ModelCatalog> | null = null;

export function loadModelCatalog(): Promise<ModelCatalog> {
  if (cached === null) {
    cached = fetchModelCatalog();
  }
  return cached;
}

export function __resetCatalogCacheForTest(): void {
  cached = null;
}

export function providerNames(catalog: ModelCatalog): string[] {
  return catalog.providers.map((p) => p.provider);
}

export function modelsFor(catalog: ModelCatalog, provider: string): CatalogModel[] {
  return catalog.providers.find((p) => p.provider === provider)?.models ?? [];
}

export function lookupModel(
  catalog: ModelCatalog,
  provider: string,
  name: string,
): CatalogModel | undefined {
  return modelsFor(catalog, provider).find((m) => m.name === name);
}

/** reasoning_effort vendors without a real off — off degrades to the lowest
 *  level (OpenAI/Azure "minimal", kimi-k3 "low"). GLM 5.2+ and DeepSeek keep
 *  a real off via thinking.type=disabled, so no hint there — except entries
 *  the catalog marks always_thinking (glm-5.3-flash: thinking.type only
 *  supports enabled, off floors at reasoning_effort=low). */
export function thinkingCannotFullyDisable(
  entry: CatalogModel | undefined,
  provider: string | undefined,
): boolean {
  return (
    entry?.thinking === "effort" &&
    (entry?.always_thinking === true ||
      (provider !== "anthropic" && provider !== "glm" && provider !== "deepseek"))
  );
}
