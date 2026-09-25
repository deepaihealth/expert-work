/**
 * Model catalog SDK — Stream S PR D (Mini-ADR S-4 client side).
 *
 * ``GET /v1/model-catalog`` returns the selectable models per *configured*
 * provider (the backend already intersects the catalog with platform
 * credentials and drops deprecated models), inside the standard envelope.
 */
import { getJson } from "./client";

export interface CatalogModel {
  name: string;
  vision: boolean;
  embeddings: boolean;
  context_window: number | null;
  deprecated: boolean;
  // Thinking-Toggle — the vendor's runtime thinking-control shape (null = no
  // knob → no switch) + the model's default thinking state (seeds the switch).
  thinking?: "effort" | "budget" | "toggle" | null;
  thinking_default?: boolean;
  // Per-model "cannot turn thinking off at all" flag (glm-5.3-flash) — the
  // config UI shows the not-fully-off hint even though the provider
  // otherwise has a real off.
  always_thinking?: boolean;
  // B-105 — output-cap wire field the agent factory sends this model
  // (informational; the form doesn't branch on it).
  output_cap_field?: "max_tokens" | "max_completion_tokens" | "split";
  // B-105 — vendor ceiling for a single reply's output tokens (thinking
  // included), null = no known ceiling. Drives the output-cap input's
  // placeholder + max.
  max_output_tokens?: number | null;
  // B-105 — whether this model has a REAL thinking-length hard cap (vs. only
  // discrete effort/toggle levels). Gates the thinking-length-cap input —
  // the backend rejects thinking_max_tokens on a model where this is false.
  thinking_cap?: boolean;
}

export interface ProviderModels {
  provider: string;
  models: CatalogModel[];
}

export interface ModelCatalog {
  providers: ProviderModels[];
}

export async function fetchModelCatalog(): Promise<ModelCatalog> {
  return getJson<ModelCatalog>("/v1/model-catalog");
}
