"""MODEL_CATALOG shape + lookup — Stream S PR B (Mini-ADR S-4)."""

from expert_work.protocol import (
    MODEL_CATALOG,
    ModelEntry,
    catalog_entry,
    models_for_provider,
)
from expert_work.protocol.provider_catalog import PROVIDER_CATALOG


def test_catalog_keys_are_known_providers() -> None:
    for provider in MODEL_CATALOG:
        assert provider in PROVIDER_CATALOG


def test_entries_are_model_entry_with_required_fields() -> None:
    for entries in MODEL_CATALOG.values():
        for e in entries:
            assert isinstance(e, ModelEntry)
            assert e.name
            assert isinstance(e.vision, bool)
            assert isinstance(e.embeddings, bool)


def test_deepseek_legacy_aliases_deprecated_but_resolvable() -> None:
    # deepseek-chat / deepseek-reasoner are the retired V3-era aliases (vendor
    # retirement 2026-07-24): dropped from the dropdown but still resolvable via
    # catalog_entry so in-flight manifests keep working through the transition.
    dropdown = {e.name for e in models_for_provider("deepseek")}
    assert "deepseek-chat" not in dropdown
    assert "deepseek-reasoner" not in dropdown
    # The current versioned models are what the dropdown offers.
    assert {"deepseek-v4-pro", "deepseek-v4-flash"} <= dropdown
    for alias in ("deepseek-chat", "deepseek-reasoner"):
        entry = catalog_entry("deepseek", alias)
        assert entry is not None and entry.deprecated is True and entry.vision is False


def test_models_for_provider_excludes_deprecated() -> None:
    for e in models_for_provider("anthropic"):
        assert e.deprecated is False


def test_models_for_unknown_provider_is_empty() -> None:
    assert models_for_provider("not-a-provider") == ()


def test_required_embedding_and_rerank_models_present() -> None:
    glm = {e.name: e for e in MODEL_CATALOG["glm"]}
    qwen = {e.name: e for e in MODEL_CATALOG["qwen"]}
    assert glm["embedding-3"].embeddings is True
    assert qwen["text-embedding-v4"].embeddings is True
    assert qwen["qwen3-vl-rerank"].rerank is True


def test_model_entry_has_rerank_flag_defaulting_false() -> None:
    e = ModelEntry(name="x")
    assert e.rerank is False


# ---------------------------------------------------------------------------
# CM-9 — compute-control capability bits + catalog_entry lookup
# ---------------------------------------------------------------------------


def test_anthropic_capability_bits() -> None:
    opus = catalog_entry("anthropic", "claude-opus-4-8")
    sonnet = catalog_entry("anthropic", "claude-sonnet-4-6")
    haiku = catalog_entry("anthropic", "claude-haiku-4-5")
    assert opus is not None and opus.thinking == "effort" and not opus.sampling
    assert sonnet is not None and sonnet.thinking == "effort" and sonnet.sampling
    assert haiku is not None and haiku.thinking is None and haiku.sampling


def test_catalog_entry_off_catalog_returns_none() -> None:
    assert catalog_entry("anthropic", "claude-imaginary-9") is None
    assert catalog_entry("nonexistent-provider", "x") is None


def test_current_context_windows() -> None:
    """Lock in the context windows verified against each vendor's official docs
    (2026-07). These feed ``_resolved_context_window`` → the compression /
    working-window thresholds (``0.7 x window``), so a stale-low value silently
    over-compresses long runs. Re-verify against vendor docs when a value here
    changes."""
    expected = {
        # Flagships confirmed 1M against official docs (Anthropic 1M GA on 4.8;
        # OpenAI GPT-5.5 API window; DeepSeek V4; GLM-5.2 bigmodel; Qwen 3.7).
        ("anthropic", "claude-opus-4-8"): 1_000_000,
        ("anthropic", "claude-sonnet-4-6"): 1_000_000,
        ("anthropic", "claude-haiku-4-5"): 200_000,
        ("openai", "gpt-5.5"): 1_000_000,
        ("openai", "gpt-5.5-pro"): 1_000_000,
        ("openai", "gpt-5.4-mini"): 400_000,
        ("deepseek", "deepseek-v4-pro"): 1_000_000,
        ("deepseek", "deepseek-v4-flash"): 1_000_000,
        # GLM-5.3: 1M per the bigmodel pricing page (2026-08). GLM-5V-Turbo:
        # 200K per the bigmodel VLM docs (2026-08).
        ("glm", "glm-5.3"): 1_000_000,
        ("glm", "glm-5v-turbo"): 200_000,
        ("glm", "glm-5.2"): 1_000_000,
        ("glm", "glm-5.1"): 200_000,
        ("glm", "glm-4.7"): 200_000,
        ("glm", "glm-4.6"): 200_000,
        ("kimi", "kimi-k3"): 1_000_000,
        ("kimi", "kimi-k2.6"): 256_000,
        ("kimi", "kimi-k2.5"): 256_000,
        # Qwen3.8-Max: 1M (2.4T MoE, 2026-08 launch — Model Studio).
        ("qwen", "qwen3.8-max"): 1_000_000,
        ("qwen", "qwen3.7-max"): 1_000_000,
        ("qwen", "qwen3.6-plus"): 1_000_000,
        ("doubao", "doubao-seed-2-1-pro-260628"): 256_000,
    }
    for (provider, name), window in expected.items():
        entry = catalog_entry(provider, name)
        assert entry is not None, f"{provider}/{name} missing from catalog"
        assert entry.context_window == window, (
            f"{provider}/{name}: catalog {entry.context_window} != verified {window}"
        )


def test_kimi_k3_capability_bits() -> None:
    # kimi-k3: native vision, 1M context. thinking is "effort" — K3 is always
    # thinking with a top-level reasoning_effort on the max/high/low scale
    # (default max, platform.kimi.com thinking docs 2026-08); the adapter maps
    # the unified levels and floors "off" at low (no off switch on K3).
    k3 = catalog_entry("kimi", "kimi-k3")
    assert k3 is not None
    assert k3.vision is True
    assert k3.context_window == 1_000_000
    assert k3.thinking == "effort"
    assert k3.thinking_default is True
    assert k3.deprecated is False
    # It is the current flagship — selectable in the dropdown.
    assert "kimi-k3" in {e.name for e in models_for_provider("kimi")}


def test_2026_08_additions_capability_bits() -> None:
    """2026-08 additions verified against vendor docs: glm-5.3 (text, 1M,
    reasoning_effort per the bigmodel core-params page — GLM-5.2 及以上),
    glm-5v-turbo (vision Agent base, 200K, toggle), qwen3.8-max (natively
    multimodal, 1M, enable_thinking budget)."""
    glm53 = catalog_entry("glm", "glm-5.3")
    assert glm53 is not None
    assert glm53.vision is False and glm53.thinking == "effort"
    glm52 = catalog_entry("glm", "glm-5.2")
    assert glm52 is not None
    assert glm52.thinking == "effort"
    # GLM ≤5.1 stays on/off only — the effort scale is 5.2+.
    glm51 = catalog_entry("glm", "glm-5.1")
    assert glm51 is not None
    assert glm51.thinking == "toggle"
    glm5v = catalog_entry("glm", "glm-5v-turbo")
    assert glm5v is not None
    assert glm5v.vision is True and glm5v.thinking == "toggle"
    q38 = catalog_entry("qwen", "qwen3.8-max")
    assert q38 is not None
    assert q38.vision is True and q38.thinking == "budget"
    # All three are current — selectable in the dropdown.
    assert {"glm-5.3", "glm-5v-turbo"} <= {e.name for e in models_for_provider("glm")}
    assert "qwen3.8-max" in {e.name for e in models_for_provider("qwen")}


def test_glm_53_flash_capability_bits() -> None:
    """glm-5.3-flash (bigmodel docs 2026-08): text params同 glm-5.3 (1M,
    effort), natively multimodal input, and thinking.type 仅支持 enabled —
    the first ``always_thinking`` entry (off floors at the lowest effort
    tier; clear_thinking deliberately not sent, see catalog comment)."""
    flash = catalog_entry("glm", "glm-5.3-flash")
    assert flash is not None
    assert flash.vision is True
    assert flash.context_window == 1_000_000
    assert flash.thinking == "effort"
    assert flash.thinking_default is True
    assert flash.always_thinking is True
    assert "glm-5.3-flash" in {e.name for e in models_for_provider("glm")}
    # The flag defaults False everywhere else — the provider-level "real off"
    # derivation stays intact for glm-5.2 and deepseek. (B-105 2026-09-24 实调:
    # glm-5.3 itself turned out to ALSO be always_thinking — see
    # test_catalog_corrections_from_live_probe — so glm-5.2 replaces it here as
    # the still-真-off example.)
    assert catalog_entry("glm", "glm-5.2").always_thinking is False  # type: ignore[union-attr]
    assert catalog_entry("deepseek", "deepseek-v4-pro").always_thinking is False  # type: ignore[union-attr]


def test_cross_vendor_thinking_shapes() -> None:
    """CM-10 (Mini-ADR CM-L1) — thinking capability shapes per vendor."""
    assert catalog_entry("openai", "gpt-5.5").thinking == "effort"  # type: ignore[union-attr]
    assert catalog_entry("deepseek", "deepseek-v4-pro").thinking == "effort"  # type: ignore[union-attr]
    assert catalog_entry("qwen", "qwen3.7-max").thinking == "budget"  # type: ignore[union-attr]
    # B-105(2026-09-24 实调)—— doubao ``thinking.budget_tokens`` 被厂商忽略,真正
    # 生效的是 ``reasoning_effort`` → shape "effort"(不再是 "budget")。
    assert catalog_entry("doubao", "doubao-seed-2.0-pro").thinking == "effort"  # type: ignore[union-attr]
    assert catalog_entry("glm", "glm-5.1").thinking == "toggle"  # type: ignore[union-attr]
    assert catalog_entry("kimi", "kimi-k2.6").thinking == "toggle"  # type: ignore[union-attr]
    # Always-thinking / no-control models stay None.
    assert catalog_entry("deepseek", "deepseek-reasoner").thinking is None  # type: ignore[union-attr]
    assert catalog_entry("qwen", "text-embedding-v4").thinking is None  # type: ignore[union-attr]


def test_thinking_defaults_none() -> None:
    assert ModelEntry(name="x").thinking is None


def test_thinking_default_field() -> None:
    # Thinking-Toggle — field defaults False; every in-sale thinking-capable
    # model declares its real default. Most default thinking ON; B-105
    # (2026-09-24 实调) found qwen3-vl-plus / qwen3-vl-flash default OFF
    # (enable_thinking is a real opt-in there), and no-knob models keep the
    # False default.
    default_off = {("qwen", "qwen3-vl-plus"), ("qwen", "qwen3-vl-flash")}
    assert ModelEntry(name="x").thinking_default is False
    for provider, models in MODEL_CATALOG.items():
        for entry in models:
            if entry.thinking is not None:
                expected = (provider, entry.name) not in default_off
                assert entry.thinking_default is expected, f"{provider}/{entry.name}"
            else:
                assert entry.thinking_default is False, f"{provider}/{entry.name}"


# --- Stream HX-13 — tool_disclosure capability bit --------------------------


def test_tool_disclosure_defaults_to_none() -> None:
    assert ModelEntry(name="x").tool_disclosure is None


def test_tool_disclosure_catalog_annotations() -> None:
    """HX-13 tier annotations: anthropic mainline → native_search; OpenAI
    current chat models → allowed_tools; haiku / embeddings / deprecated /
    compat vendors stay None (the HX-12 application tier)."""
    from expert_work.protocol.model_catalog import MODEL_CATALOG, catalog_entry

    def _entry(provider: str, name: str) -> ModelEntry:
        entry = catalog_entry(provider, name)
        assert entry is not None
        return entry

    assert _entry("anthropic", "claude-opus-4-8").tool_disclosure == "native_search"
    assert _entry("anthropic", "claude-sonnet-4-6").tool_disclosure == "native_search"
    assert _entry("anthropic", "claude-haiku-4-5").tool_disclosure is None
    assert _entry("openai", "gpt-5.5").tool_disclosure == "allowed_tools"
    assert _entry("openai", "text-embedding-3-large").tool_disclosure is None
    assert _entry("openai", "gpt-4o").tool_disclosure is None
    # Compat vendors are unverified → None across the board (CM-L5).
    for provider in ("kimi", "glm", "deepseek", "qwen", "doubao"):
        for entry in MODEL_CATALOG[provider]:
            assert entry.tool_disclosure is None, (provider, entry.name)


def test_temperature_fixed_declared_only_where_documented() -> None:
    """B-33 — kimi-k3 accepts only ``temperature=1`` (platform.kimi.com docs;
    confirmed on the live stack 2026-08-28: any other value is a vendor 400
    that killed the whole run). The catalog declares the constraint so the
    factory clamps at build time instead of letting the vendor 400 act as the
    validator. No other entry has documented evidence, so every other entry
    stays ``None`` (manifest value sent as-is)."""
    k3 = catalog_entry("kimi", "kimi-k3")
    assert k3 is not None
    assert k3.temperature_fixed == 1.0
    constrained = [
        (provider, entry.name)
        for provider, entries in MODEL_CATALOG.items()
        for entry in entries
        if entry.temperature_fixed is not None
    ]
    assert constrained == [("kimi", "kimi-k3")]
    # Field default — an entry that says nothing constrains nothing.
    assert ModelEntry(name="x").temperature_fixed is None


# ---------------------------------------------------------------------------
# B-105 — output-cap field / vendor output ceiling / thinking-length cap
# ---------------------------------------------------------------------------


def _e(provider: str, name: str) -> ModelEntry:
    entry = catalog_entry(provider, name)
    assert entry is not None, name
    return entry


def test_output_cap_fields_match_live_probe_2026_09_24() -> None:
    # spec §8.1 / §8.7 —— 设错字段在 GLM / DeepSeek 上是静默的,这张表只能靠测试守。
    for name in ("glm-5.3", "glm-5.3-flash", "glm-5.2", "glm-5.1", "glm-4.7", "glm-4.6",
                 "glm-5v-turbo", "glm-4.6v", "glm-4.5v"):
        assert _e("glm", name).output_cap_field == "max_tokens"
    for name in ("deepseek-v4-pro", "deepseek-v4-flash"):
        assert _e("deepseek", name).output_cap_field == "max_tokens"
    for name in ("kimi-k3", "kimi-k2.6", "kimi-k2.5"):
        assert _e("kimi", name).output_cap_field == "max_completion_tokens"
    for name in ("qwen3.8-max", "qwen3.7-max", "qwen3.6-plus", "qwen3.5-plus"):
        assert _e("qwen", name).output_cap_field == "max_completion_tokens"
    for name in ("qwen3-max", "qwen3-vl-plus", "qwen3-vl-flash"):
        assert _e("qwen", name).output_cap_field == "split"
    for name in ("doubao-seed-2-1-pro-260628", "doubao-seed-2.0-pro", "doubao-seed-2.0-lite"):
        assert _e("doubao", name).output_cap_field == "max_completion_tokens"
    for name in ("gpt-5.5", "gpt-5.5-pro", "gpt-5.4-mini"):
        assert _e("openai", name).output_cap_field == "max_completion_tokens"
    assert _e("anthropic", "claude-opus-4-8").output_cap_field == "max_tokens"


def test_max_output_tokens_match_vendor_ranges() -> None:
    for name in ("glm-5.3", "glm-5.3-flash", "glm-5.2", "glm-5.1", "glm-4.7", "glm-4.6", "glm-5v-turbo"):
        assert _e("glm", name).max_output_tokens == 131_072
    assert _e("glm", "glm-4.6v").max_output_tokens == 32_768
    assert _e("glm", "glm-4.5v").max_output_tokens == 16_384
    assert _e("deepseek", "deepseek-v4-pro").max_output_tokens == 393_216
    assert _e("deepseek", "deepseek-v4-flash").max_output_tokens == 393_216
    assert _e("doubao", "doubao-seed-2-1-pro-260628").max_output_tokens == 262_144
    assert _e("kimi", "kimi-k3").max_output_tokens is None
    assert _e("qwen", "qwen3.8-max").max_output_tokens is None


def test_thinking_cap_only_on_qwen_thinking_models() -> None:
    capped = {"qwen3.8-max", "qwen3.7-max", "qwen3.6-plus", "qwen3.5-plus", "qwen3-max",
              "qwen3-vl-plus", "qwen3-vl-flash"}
    from expert_work.protocol.model_catalog import MODEL_CATALOG
    for provider, entries in MODEL_CATALOG.items():
        for e in entries:
            assert e.thinking_cap is (provider == "qwen" and e.name in capped), e.name


def test_catalog_corrections_from_live_probe() -> None:
    # glm-5.3 关思考 400(「该模型始终思考」)。
    assert _e("glm", "glm-5.3").always_thinking is True
    # 豆包 budget_tokens 被厂商忽略 → 档位走 reasoning_effort;没有 max 档。
    for name in ("doubao-seed-2-1-pro-260628", "doubao-seed-2.0-pro", "doubao-seed-2.0-lite"):
        e = _e("doubao", name)
        assert e.thinking == "effort"
        assert e.effort_map == {"max": "high"}
    # glm-4.6v / 4.5v 实际默认思考、disabled 有效。
    for name in ("glm-4.6v", "glm-4.5v"):
        e = _e("glm", name)
        assert (e.thinking, e.thinking_default) == ("toggle", True)
    # qwen3-vl 默认不思考、enable_thinking 有效。
    for name in ("qwen3-vl-plus", "qwen3-vl-flash"):
        e = _e("qwen", name)
        assert (e.thinking, e.thinking_default) == ("budget", False)


def test_max_high_low_effort_map_moved_into_catalog() -> None:
    mhl = {"medium": "high"}
    for name in ("glm-5.3", "glm-5.3-flash", "glm-5.2"):
        assert _e("glm", name).effort_map == mhl
    assert _e("kimi", "kimi-k3").effort_map == mhl
