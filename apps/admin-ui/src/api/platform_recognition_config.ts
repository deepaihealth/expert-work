/**
 * Platform document-recognition config SDK — backed by
 * /v1/platform/recognition-config (B-209 §5.7). system_admin-only,
 * platform-level: the models the knowledge base reads scanned PDF pages with
 * (primary + fallback), the figure-description model, and the per-document
 * recognition page cap. A ``null`` slot = that model is switched off.
 */
import { getJson, putJson } from "./client";

export interface ProviderModel {
  provider: string;
  model: string;
}

export interface PlatformRecognitionConfig {
  ocr: ProviderModel | null;
  fallback: ProviderModel | null;
  figure: ProviderModel | null;
  max_pages: number;
  /** No saved row yet — the built-in defaults are in effect. */
  is_default: boolean;
}

export interface PlatformRecognitionConfigView extends PlatformRecognitionConfig {
  /** Models declaring recognition capability, for configured providers. */
  available_ocr: ProviderModel[];
  /** Vision models that take a prompt, for configured providers. */
  available_figure: ProviderModel[];
}

export interface PlatformRecognitionConfigWrite {
  ocr_provider: string | null;
  ocr_model: string | null;
  fallback_provider: string | null;
  fallback_model: string | null;
  figure_provider: string | null;
  figure_model: string | null;
  max_pages: number;
}

export async function getPlatformRecognitionConfig(): Promise<PlatformRecognitionConfigView> {
  return getJson<PlatformRecognitionConfigView>("/v1/platform/recognition-config");
}

export async function putPlatformRecognitionConfig(
  body: PlatformRecognitionConfigWrite,
): Promise<PlatformRecognitionConfig> {
  return putJson("/v1/platform/recognition-config", body);
}
