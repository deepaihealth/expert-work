/**
 * English translations — Stream H.1b PR 2a.
 *
 * Glossary terms (Agent / Run / Skill / Trigger / Manifest / Memory /
 * Curation / Eval / Tenant / API Key / Audit / Sandbox / Service
 * Account / Playground / Trace / Span / Quota / Volume) are intentionally
 * kept un-translated in both locales — operators reading Expert Work docs +
 * runtime UI in two languages benefit from a single canonical noun.
 */
export interface TranslationKeys {
  common: {
    sign_in: string;
    sign_out: string;
    refresh: string;
    loading: string;
    cancel: string;
    confirm: string;
    save: string;
    delete: string;
    edit: string;
    search_or_jump: string;
    notifications: string;
    user_menu: string;
    anonymous: string;
    field_help: string;
    copy: string;
    copied: string;
    tenant_switched_readonly: string;
    cross_tenant_readonly: string;
  };
  theme: {
    switch_to_light: string;
    switch_to_dark: string;
    toggle: string;
  };
  nav: {
    settings_group: string;
    group_workspace: string;
    group_tenant_settings: string;
    group_platform: string;
    members_all_tenants: string;
    agents: string;
    conversations: string;
    users: string;
    approvals: string;
    curation: string;
    eval: string;
    memory: string;
    knowledge: string;
    skills: string;
    agent_template_marketplace: string;
    triggers: string;
    webhooks: string;
    tenants: string;
    platform_credentials: string;
    platform_users: string;
    api_keys: string;
    credentials: string;
    service_accounts: string;
    members: string;
    audit: string;
    egress_audit: string;
    mcp_servers: string;
    mcp_oauth: string;
    mcp_catalog: string;
    agent_templates: string;
    platform_skills: string;
    usage: string;
    chargeback: string;
    rate_card: string;
    observability: string;
    keycloak: string;
    quality: string;
  };
  quality_page: {
    page_title: string;
    subtitle: string;
    honest_note: string;
    filter_agent: string;
    filter_window: string;
    window_24h: string;
    window_7d: string;
    window_30d: string;
    failed_to_load: string;
    section_drift: string;
    section_trend: string;
    section_low_scores: string;
    col_agent: string;
    col_tenant: string;
    col_overall: string;
    col_dimensions: string;
    col_rationale: string;
    col_observed_at: string;
    col_run: string;
    open_run: string;
    trend_mean: string;
    trend_samples: string;
    trend_empty: string;
    low_empty: string;
    drift_empty: string;
    drift_col_recent: string;
    drift_col_baseline: string;
    drift_col_drift_pct: string;
    drift_col_samples: string;
    drift_col_detected_at: string;
  };
  observability_page: {
    page_title: string;
    subtitle: string;
    not_admin_title: string;
    not_admin_body: string;
    tenant_isolation_note: string;
    open: string;
    unconfigured: string;
    langfuse_name: string;
    langfuse_desc: string;
    grafana_name: string;
    grafana_desc: string;
    tempo_name: string;
    tempo_desc: string;
  };
  keycloak_page: {
    page_title: string;
    subtitle: string;
    not_admin_title: string;
    not_admin_body: string;
    console_name: string;
    console_desc: string;
    open: string;
    unconfigured: string;
  };
  login: {
    title: string;
    paragraph: string;
    token_label: string;
    token_placeholder: string;
    token_required: string;
    token_empty: string;
    pr2_hint: string;
    sign_in_sso: string;
    sso_help: string;
    dev_login_toggle: string;
    dev_login_hide: string;
    dev_login_section: string;
  };
  auth_callback: {
    title: string;
    exchanging: string;
    failed: string;
  };
  setup: {
    title: string;
    paragraph: string;
    platform_name_label: string;
    admin_email_label: string;
    admin_email_required: string;
    admin_email_invalid: string;
    admin_password_label: string;
    admin_password_required: string;
    admin_password_length: string;
    admin_password_confirm_label: string;
    admin_password_confirm_required: string;
    admin_password_mismatch: string;
    admin_display_name_label: string;
    setup_token_label: string;
    setup_token_hint: string;
    setup_token_required: string;
    notice: string;
    submit: string;
    success_toast: string;
    done_title: string;
    done_subtitle: string;
    go_to_login: string;
    already_initialized_title: string;
    already_initialized_subtitle: string;
    error_already_initialized: string;
    error_invalid_token: string;
    error_not_configured: string;
    error_email_exists: string;
    error_keycloak_unavailable: string;
    error_generic: string;
  };
  agent_detail: {
    failed_to_load: string;
    tab_overview: string;
    tab_manifest: string;
    tab_playground: string;
    tab_users: string;
    tab_conversations: string;
    tab_skills: string;
    tab_triggers: string;
    tab_history: string;
    tab_coming_soon: string;
    config_summary: string;
    field_id: string;
    field_tenant: string;
    field_spec_sha: string;
    field_status: string;
    field_created: string;
    field_updated: string;
    egress_title: string;
    egress_policy: string;
    egress_allowlist: string;
    egress_isolated: string;
    egress_proxied: string;
    egress_allow_all: string;
    disabled_tag: string;
    disabled_reason: string;
    disable: string;
    enable: string;
    disable_confirm_title: string;
    disable_confirm_body: string;
    disable_reason_placeholder: string;
    enable_confirm: string;
    disable_ok: string;
    enable_ok: string;
    disable_failed: string;
    enable_failed: string;
  };
  history_tab: {
    title: string;
    select_hint: string;
    current: string;
    col_revision: string;
    col_sha: string;
    col_actor: string;
    col_time: string;
    rollback: string;
    rollback_confirm_title: string;
    rollback_confirm_body: string;
    diff_label: string;
    diff_loading: string;
  };
  conversations_tab: {
    title: string;
    empty: string;
  };
  users_tab: {
    title: string;
    empty: string;
    unnamed: string;
    column_user: string;
    column_conversations: string;
    column_runs: string;
    column_tokens: string;
    column_last_active: string;
  };
  user_detail: {
    tab_conversations: string;
    tab_memory: string;
    tab_artifacts: string;
    tab_usage: string;
    conversations_empty: string;
    memory_scope_note: string;
    memory_empty: string;
    artifacts_scope_note: string;
    artifacts_empty: string;
    artifact_name: string;
    artifact_kind: string;
    artifact_version: string;
    usage_scope_note: string;
    usage_month: string;
    usage_total: string;
    usage_in_out: string;
    usage_by_model: string;
  };
  users_page: {
    page_title: string;
    subtitle: string;
    not_admin_title: string;
    not_admin_body: string;
    stat_total: string;
    stat_conversations: string;
    stat_errors: string;
    search_placeholder: string;
    type_all: string;
    type_external: string;
    type_member: string;
    truncated: string;
    truncated_tip: string;
    col_user: string;
    col_type: string;
    col_conversations: string;
    col_runs: string;
    col_errors: string;
    col_last_active: string;
    tag_external: string;
    tag_member: string;
    failed_to_load: string;
    empty: string;
  };
  user_profile: {
    back_label: string;
    banner: string;
    tab_conversations: string;
    tab_memory: string;
    tab_workspace: string;
    tab_usage: string;
    subject_id_label: string;
    surrogate_label: string;
    filter_agent: string;
    filter_agent_all: string;
    filter_range: string;
    conversations_empty: string;
    memory_col_importance: string;
    memory_col_confidence: string;
    memory_col_actions: string;
    memory_edit: string;
    memory_edit_title: string;
    memory_edit_content: string;
    memory_edit_kind: string;
    memory_forget: string;
    memory_forget_confirm: string;
    memory_updated: string;
    memory_forgotten: string;
    workspace_scope_note: string;
    workspace_volume: string;
    workspace_size: string;
    workspace_usage: string;
    workspace_none: string;
    workspace_files: string;
    workspace_files_empty: string;
    download: string;
    delete: string;
    delete_confirm: string;
    deleted: string;
    failed_to_load: string;
    purge_action: string;
    purge_title: string;
    purge_warning: string;
    purge_self_warning: string;
    purge_deletes: string;
    purge_anonymizes: string;
    purge_archive_note: string;
    purge_type_to_confirm: string;
    purge_confirm_btn: string;
    purge_done: string;
    purge_partial: string;
    purge_employee_title: string;
    purge_employee_body: string;
    purge_failed_title: string;
  };
  conversations_page: {
    page_title: string;
    subtitle: string;
    untitled: string;
    column_conversation: string;
    column_agent: string;
    column_user: string;
    column_status: string;
    column_runs: string;
    column_tokens: string;
    column_last_active: string;
    filter_status: string;
    filter_status_all: string;
    filter_user_tip: string;
    filter_user_active: string;
    filter_agent: string;
    filter_errors_only: string;
    filter_pending_only: string;
    auto_refresh: string;
    filter_window: string;
    window_all: string;
    window_1h: string;
    window_24h: string;
    window_7d: string;
    pager_total: string;
    search_placeholder: string;
    cross_tenant_banner: string;
    failed_to_load: string;
    error_count: string;
    pending_count: string;
    empty_home: string;
    empty_cross: string;
  };
  conversations_detail: {
    failed_to_load: string;
    thread_label: string;
    summary_title: string;
    user: string;
    tokens: string;
    tokens_value: string;
    models: string;
    messages_title: string;
    messages_empty: string;
    role_user: string;
    role_assistant: string;
    runs_title: string;
    runs_empty: string;
    column_started: string;
    view_run: string;
  };
  skills_tab: {
    title: string;
    authored_hint: string;
    col_name: string;
    col_status: string;
    col_visibility: string;
    col_version: string;
    col_created: string;
    empty: string;
  };
  triggers_tab: {
    title: string;
    manage_hint: string;
    col_name: string;
    col_kind: string;
    col_enabled: string;
    col_source: string;
    col_created: string;
    enabled: string;
    disabled: string;
    empty: string;
  };
  memory_tab: {
    col_kind: string;
    col_content: string;
    col_created: string;
    col_source: string;
    source_run: string;
    as_of_placeholder: string;
    as_of_banner: string;
  };
  knowledge_page: {
    page_title: string;
    subtitle: string;
    failed_to_load: string;
    bases_title: string;
    bases_empty: string;
    empty_cross: string;
    col_tenant: string;
    create_base: string;
    create_duplicate: string;
    field_name: string;
    field_chunk_max: string;
    field_chunk_overlap: string;
    field_chunk_hint: string;
    col_base_name: string;
    col_chunking: string;
    delete: string;
    delete_base_confirm_title: string;
    delete_base_confirm_body: string;
    documents_title: string;
    documents_unselected: string;
    documents_empty: string;
    select_hint: string;
    upload: string;
    unsupported_type: string;
    embedder_missing: string;
    col_filename: string;
    col_status: string;
    col_chunks: string;
    col_updated: string;
    delete_doc_confirm_title: string;
    // ── commercial uplift ──
    field_description: string;
    field_description_placeholder: string;
    advanced: string;
    retrieval_defaults: string;
    field_top_k: string;
    field_top_k_hint: string;
    field_threshold: string;
    field_threshold_hint: string;
    field_method: string;
    field_method_hint: string;
    method_vector: string;
    method_keyword: string;
    method_hybrid: string;
    field_rerank: string;
    field_rerank_hint: string;
    col_description: string;
    col_documents: string;
    col_chunks_total: string;
    needs_reindex_tag: string;
    reindexing_tag: string;
    status_pending: string;
    status_processing: string;
    status_ready: string;
    status_failed: string;
    failed_to_load_base: string;
    tab_documents: string;
    tab_test: string;
    tab_settings: string;
    stat_documents: string;
    stat_chunks: string;
    stat_embedding: string;
    embedding_unset: string;
    needs_reindex_banner: string;
    reindex_button: string;
    reindex_started: string;
    upload_dragger_hint: string;
    upload_dragger_sub: string;
    col_attempts: string;
    reingest: string;
    reingest_started: string;
    reingest_no_bytes: string;
    view_chunks: string;
    chunks_drawer_title: string;
    chunks_empty: string;
    chunk_label: string;
    test_title: string;
    test_query_placeholder: string;
    test_run: string;
    test_empty_hint: string;
    test_no_results: string;
    test_control_top_k: string;
    test_control_method: string;
    test_control_threshold: string;
    test_control_rerank: string;
    test_method_default: string;
    recall_vector: string;
    recall_keyword: string;
    recall_both: string;
    settings_title: string;
    settings_saved: string;
    settings_rename_note: string;
    settings_embedding_title: string;
  };
  rate_card_page: {
    page_title: string;
    subtitle: string;
    system_admin_only: string;
    failed_to_load: string;
    empty: string;
    filter_provider: string;
    filter_model: string;
    create: string;
    edit: string;
    edit_title: string;
    delete: string;
    delete_confirm_title: string;
    delete_confirm_body: string;
    col_provider: string;
    col_model: string;
    col_input: string;
    col_output: string;
    col_cache_creation: string;
    col_cache_read: string;
    field_provider: string;
    field_model: string;
    field_input: string;
    field_output: string;
    field_cache_creation: string;
    field_cache_read: string;
    identity_immutable: string;
  };
  artifacts_page: {
    col_name: string;
    col_kind: string;
    col_latest: string;
    download: string;
    download_failed: string;
    versions: string;
    versions_title: string;
    versions_empty: string;
    ver_col_version: string;
    ver_col_path: string;
    ver_col_size: string;
    ver_col_created: string;
    digest_pending: string;
    delete: string;
    deleted: string;
    delete_confirm_title: string;
    delete_confirm_body: string;
  };
  manifest_tab: {
    hint: string;
    save: string;
    reset: string;
    save_failed: string;
  };
  manifest_editor: {
    tab_basic: string;
    tab_model: string;
    tab_prompt: string;
    tab_tools: string;
    tab_mcp: string;
    tab_knowledge: string;
    tab_skills: string;
    tab_subagents: string;
    tab_memory: string;
    tab_governance: string;
    tab_defenses: string;
    tab_yaml: string;
    loading_schema: string;
    schema_load_failed: string;
    invalid_yaml_title: string;
    invalid_yaml_hint: string;
    search_placeholder: string;
    group_basic: string;
    group_model: string;
    group_prompt: string;
    group_capabilities: string;
    group_memory: string;
    group_budget: string;
    group_context: string;
    group_security: string;
    group_sandbox: string;
    group_observability: string;
    field_impact_label: string;
    field_customized_badge: string;
    field_reset: string;
    field_reset_hint: string;
  };
  // config-page redesign v2 Task 6 — RunProfileCard, the "basic" group's
  // one-click preset over 18 fields scattered across memory/budget/context
  // (form_model.ts's RUN_PROFILES). ``{balanced,cost,capability}``/
  // ``{...}_desc`` are the three preset cards' label + one-line blurb;
  // ``custom`` is the "no preset matches" tag.
  run_profile: {
    title: string;
    hint: string;
    balanced: string;
    balanced_desc: string;
    cost: string;
    cost_desc: string;
    capability: string;
    capability_desc: string;
    custom: string;
    nearest_hint: string;
    confirm_title: string;
    confirm_body: string;
  };
  // Task 6 — pilot "Run Budget & Timeouts" group (RunBudgetSection). Six
  // FieldRow-rendered knobs living in three manifest locations
  // (workflow.max_iterations+type / policies.max_no_progress+run_deadline_s /
  // top-level spec.stream_deadline_s+idle_timeout_s).
  run_budget: {
    subhead_steps: string;
    max_iterations_label: string;
    max_iterations_brief: string;
    max_iterations_impact: string;
    max_iterations_default: string;
    wf_type_label: string;
    wf_type_brief: string;
    wf_type_impact: string;
    wf_type_default: string;
    wf_type_opt_react: string;
    wf_type_opt_plan_execute: string;
    wf_type_opt_custom: string;
    max_no_progress_label: string;
    max_no_progress_brief: string;
    max_no_progress_impact: string;
    max_no_progress_default: string;
    subhead_time: string;
    run_deadline_label: string;
    run_deadline_brief: string;
    run_deadline_impact: string;
    run_deadline_default: string;
    stream_deadline_label: string;
    stream_deadline_brief: string;
    stream_deadline_impact: string;
    stream_deadline_default: string;
    idle_timeout_label: string;
    idle_timeout_brief: string;
    idle_timeout_impact: string;
    idle_timeout_default: string;
    token_budget_label: string;
    token_budget_brief: string;
    token_budget_impact: string;
    token_budget_default: string;
  };
  // Task 3 — "Context & Compression" group (ContextGatesSection). 18 knobs
  // across the four PolicySpec sub-blocks the three context gates read
  // (tool_result_prune / working_memory / context_compression) plus the
  // sibling tool_output_budget master switch. Prefixes mirror
  // ContextGatesFields' camelCase field names (pr_/wm_/cc_/budget_).
  context_gates: {
    group_intro: string;
    // #4 — per-step one-liner shown as the first line inside each tab; the
    // detail that used to crowd ``group_intro`` moved here.
    tab_intro_prune: string;
    tab_intro_window: string;
    tab_intro_compress: string;
    panel_tool_result_prune: string;
    panel_working_memory: string;
    panel_context_compression: string;
    pr_enabled_label: string;
    pr_enabled_brief: string;
    pr_enabled_impact: string;
    pr_enabled_default: string;
    pr_threshold_pct_label: string;
    pr_threshold_pct_brief: string;
    pr_threshold_pct_impact: string;
    pr_threshold_pct_default: string;
    pr_recent_kept_label: string;
    pr_recent_kept_brief: string;
    pr_recent_kept_impact: string;
    pr_recent_kept_default: string;
    wm_enabled_label: string;
    wm_enabled_brief: string;
    wm_enabled_impact: string;
    wm_enabled_default: string;
    wm_threshold_pct_label: string;
    wm_threshold_pct_brief: string;
    wm_threshold_pct_impact: string;
    wm_threshold_pct_default: string;
    wm_max_recent_turns_label: string;
    wm_max_recent_turns_brief: string;
    wm_max_recent_turns_impact: string;
    wm_max_recent_turns_default: string;
    wm_keep_first_turn_label: string;
    wm_keep_first_turn_brief: string;
    wm_keep_first_turn_impact: string;
    wm_keep_first_turn_default: string;
    cc_enabled_label: string;
    cc_enabled_brief: string;
    cc_enabled_impact: string;
    cc_enabled_default: string;
    cc_threshold_pct_label: string;
    cc_threshold_pct_brief: string;
    cc_threshold_pct_impact: string;
    cc_threshold_pct_default: string;
    cc_head_keep_label: string;
    cc_head_keep_brief: string;
    cc_head_keep_impact: string;
    cc_head_keep_default: string;
    cc_tail_keep_label: string;
    cc_tail_keep_brief: string;
    cc_tail_keep_impact: string;
    cc_tail_keep_default: string;
    cc_flush_before_compaction_label: string;
    cc_flush_before_compaction_brief: string;
    cc_flush_before_compaction_impact: string;
    cc_flush_before_compaction_default: string;
    cc_max_passes_label: string;
    cc_max_passes_brief: string;
    cc_max_passes_impact: string;
    cc_max_passes_default: string;
    cc_pressure_feedback_label: string;
    cc_pressure_feedback_brief: string;
    cc_pressure_feedback_impact: string;
    cc_pressure_feedback_default: string;
    cc_pressure_warn_pct_label: string;
    cc_pressure_warn_pct_brief: string;
    cc_pressure_warn_pct_impact: string;
    cc_pressure_warn_pct_default: string;
    budget_enabled_label: string;
    budget_enabled_brief: string;
    budget_enabled_impact: string;
    budget_enabled_default: string;
  };
  // Task 3 (PR3) — "Security" group (SecuritySection), embedded after the
  // existing defenses/governance FormView sections. Two curated panels:
  // sandbox egress (spec.sandbox.network) and tool-use enforcement
  // (policies.tool_use_enforcement) — both default-collapsed since
  // defenses/governance remain the group's primary content. i18nKey
  // prefixes mirror SecurityFields' value keys (egress/allowlist/denylist/
  // enforce ~ toolUseEnforcement).
  security_gates: {
    tab_defenses: string;
    tab_approval: string;
    tab_network: string;
    panel_network: string;
    panel_enforce: string;
    egress_label: string;
    egress_brief: string;
    egress_impact: string;
    egress_default: string;
    egress_opt_proxy: string;
    egress_opt_direct: string;
    egress_opt_none: string;
    allowlist_label: string;
    allowlist_brief: string;
    allowlist_impact: string;
    allowlist_default: string;
    denylist_label: string;
    denylist_brief: string;
    denylist_impact: string;
    denylist_default: string;
    enforce_label: string;
    enforce_brief: string;
    enforce_impact: string;
    enforce_default: string;
    enforce_opt_auto: string;
    enforce_opt_on: string;
    enforce_opt_off: string;
    dict_note: string;
  };
  // Task 2 (PR4) — "Sandbox & Resources" group (SandboxSection). One live
  // field (persistent_workspace, the plan-projection switch) plus two note
  // blocks explaining that everything else under spec.sandbox is
  // declarative-only (validated but not read at runtime — real resource
  // limits/image/runtime are decided by the platform deployment).
  sandbox_group: {
    pw_label: string;
    pw_brief: string;
    pw_impact: string;
    platform_note_title: string;
    platform_note_intro: string;
    pn_image: string;
    pn_image_v: string;
    pn_resources: string;
    pn_resources_v: string;
    pn_timeout: string;
    pn_timeout_v: string;
    pn_paths: string;
    pn_paths_v: string;
    pn_runtime: string;
    pn_runtime_v: string;
    declarative_note: string;
  };
  // "Memory" group (MemorySection) — config-page redesign v2 Task 2 moved
  // every memory field here as three sub-tabs (basic/retrieval/budget); the
  // "memory" FormView section it used to embed is gone. ``on_*`` is the
  // long-term-memory on/off switch (presence-semantic — see form_model.ts's
  // readMemoryOn/setMemoryOn); the other 8 fields (top_k/write_back on
  // "basic", the 6 read/write tuning knobs on "retrieval") each get the
  // usual ``_label``/``_brief``/``_impact`` triple. The "budget" tab keeps
  // its two pre-existing panels: ① injection budgets
  // (spec.memory.long_term.injection_token_budget /
  // correction_token_budget — only rendered while memory is on, since a
  // budget is meaningless with no long_term block to inject from) and ②
  // background consolidation (policies.memory_consolidation.enabled — a
  // control-plane job, not on the run path, so it stays available even with
  // memory off).
  memory_group: {
    tab_basic: string;
    tab_retrieval: string;
    tab_budget: string;
    on_label: string;
    on_brief: string;
    on_impact: string;
    topk_label: string;
    topk_brief: string;
    topk_impact: string;
    write_back_label: string;
    write_back_brief: string;
    write_back_impact: string;
    verify_reads_label: string;
    verify_reads_brief: string;
    verify_reads_impact: string;
    write_min_importance_label: string;
    write_min_importance_brief: string;
    write_min_importance_impact: string;
    reconcile_writes_label: string;
    reconcile_writes_brief: string;
    reconcile_writes_impact: string;
    recall_mode_label: string;
    recall_mode_brief: string;
    recall_mode_impact: string;
    recall_mode_per_session: string;
    recall_mode_per_turn: string;
    rewrite_reads_label: string;
    rewrite_reads_brief: string;
    rewrite_reads_impact: string;
    abstain_threshold_label: string;
    abstain_threshold_brief: string;
    abstain_threshold_impact: string;
    panel_injection: string;
    panel_consolidation: string;
    inj_budget_label: string;
    inj_budget_brief: string;
    inj_budget_impact: string;
    inj_budget_default: string;
    corr_budget_label: string;
    corr_budget_brief: string;
    corr_budget_impact: string;
    corr_budget_default: string;
    consolidation_label: string;
    consolidation_brief: string;
    consolidation_impact: string;
    aux_model_note: string;
  };
  // Task 3 (PR6) — "Model & Routing" group (ModelRoutingSection), embedded
  // after the existing "model" FormView section (provider/model picker,
  // fallback chain, reflection-evaluator model picker, vision fallback). One
  // default-collapsed panel: reflection self-assessment (spec.reflection —
  // Stream J.11's reflect-node config), whose activation switch is
  // presence-semantic (see form_model.ts's readReflectionOn/setReflectionOn)
  // and whose two tuning fields (budget/deadline_s) only render once it's
  // on. A closing note flags the model-group fields still YAML-only
  // (routing.rules planning rule / vision.fallbacks / base_url and azure_*
  // wiring / the deprecated api_key_ref).
  model_group: {
    panel_reflection: string;
    rf_enable_label: string;
    rf_enable_brief: string;
    rf_enable_impact: string;
    rf_budget_label: string;
    rf_budget_brief: string;
    rf_budget_impact: string;
    rf_budget_default: string;
    rf_deadline_label: string;
    rf_deadline_brief: string;
    rf_deadline_impact: string;
    rf_deadline_default: string;
    yaml_note: string;
  };
  // Task 2 (PR7) — "Triggers & Observability" group (ObservabilitySection).
  // Two live fields (spec.cache.enabled, the per-agent LLM response cache
  // opt-out; policies.trajectory_recording, whether a finished run's full
  // transcript is archived for quality evaluation — Task 4 wrongly removed
  // its toggle as declarative-only, Task 8b restored it) plus two note
  // blocks: manifest ``triggers`` declared but not wired up (the
  // trigger-management API is the real path), and trace/log_level/
  // redact_fields declarative-only — validated but not read at runtime.
  observability_group: {
    resp_cache_label: string;
    resp_cache_brief: string;
    resp_cache_impact: string;
    traj_rec_label: string;
    traj_rec_brief: string;
    traj_rec_impact: string;
    triggers_note: string;
    declarative_note: string;
  };
  model_select: {
    provider_label: string;
    provider_placeholder: string;
    model_label: string;
    model_placeholder: string;
    vision_on: string;
    vision_off: string;
    temperature: string;
    advanced: string;
    context_window: string;
    context_window_hint: string;
    thinking_label: string;
    thinking_cannot_disable: string;
    effort_label: string;
    effort_hint: string;
    adaptive_label: string;
    adaptive_hint: string;
    cache_label: string;
    cache_hint: string;
    max_tokens_hint: string;
    rate_limit_hint: string;
  };
  agent_form: {
    section_basic: string;
    field_name: string;
    field_name_required: string;
    field_name_placeholder: string;
    field_description: string;
    basic_yaml_note: string;
    section_model: string;
    section_prompt: string;
    field_prompt_placeholder: string;
    section_tools: string;
    tool_web_search: string;
    tool_http: string;
    tool_mcp: string;
    mcp_servers_label: string;
    mcp_server_search: string;
    mcp_tool_search: string;
    mcp_scope_label: string;
    mcp_scope_all: string;
    mcp_scope_specific: string;
    mcp_select_all: string;
    mcp_clear: string;
    mcp_selected_count: string;
    mcp_choose_tools: string;
    mcp_done: string;
    mcp_all_tools: string;
    mcp_tools_count: string;
    mcp_servers_hint: string;
    mcp_no_servers_available: string;
    mcp_no_servers_catalog: string;
    mcp_source_platform: string;
    mcp_source_tenant: string;
    mcp_tools_label: string;
    mcp_tools_hint: string;
    mcp_tools_loading: string;
    mcp_tools_unreachable: string;
    mcp_servers_loading: string;
    mcp_servers_load_failed: string;
    section_reflection_evaluator: string;
    reflection_evaluator_hint: string;
    reflection_evaluator_clear: string;
    section_vision: string;
    vision_hint: string;
    vision_clear: string;
    section_vision_help: string;
    section_fallback: string;
    section_fallback_help: string;
    fallback_hint: string;
    fallback_empty: string;
    fallback_rank: string;
    fallback_add: string;
    fallback_remove: string;
    field_name_help: string;
    field_description_help: string;
    section_model_help: string;
    section_prompt_help: string;
    section_reflection_evaluator_help: string;
    section_defenses: string;
    section_defenses_help: string;
    defenses_extends_note: string;
    defenses_group_input: string;
    defenses_group_output: string;
    defenses_group_action: string;
    defenses_prompt_injection: string;
    defenses_prompt_injection_brief: string;
    defenses_prompt_injection_help: string;
    defenses_prompt_injection_off_warn: string;
    defenses_output_screen: string;
    defenses_output_screen_brief: string;
    defenses_output_screen_help: string;
    defenses_output_screen_off_warn: string;
    defenses_output_judge: string;
    defenses_output_judge_brief: string;
    defenses_output_judge_help: string;
    defenses_output_judge_on_warn: string;
    defenses_output_judge_on_error: string;
    defenses_output_dlp: string;
    defenses_output_dlp_brief: string;
    defenses_output_dlp_help: string;
    defenses_output_dlp_on_note: string;
    defenses_action_screen: string;
    defenses_action_screen_brief: string;
    defenses_action_screen_help: string;
    defenses_action_screen_off: string;
    defenses_action_screen_block: string;
    defenses_action_screen_approval: string;
    defenses_action_screen_on_note: string;
    defenses_action_screen_on_error: string;
    defenses_on_error_open: string;
    defenses_on_error_closed: string;
    section_tools_help: string;
    section_mcp: string;
    section_mcp_help: string;
    tool_web_search_help: string;
    tool_http_help: string;
    tool_mcp_help: string;
    tool_exec_python: string;
    tool_exec_python_help: string;
    tool_bash: string;
    tool_bash_help: string;
    tool_manage_task: string;
    tool_manage_task_help: string;
    tool_author_skill: string;
    tool_author_skill_help: string;
    tool_refine_skill: string;
    tool_refine_skill_help: string;
    tool_fork_skill: string;
    tool_fork_skill_help: string;
    tool_propose_skill: string;
    tool_propose_skill_help: string;
    tool_note_behavior_patch: string;
    tool_note_behavior_patch_help: string;
    tool_clarify_tool_usage: string;
    tool_clarify_tool_usage_help: string;
    tools_config_note: string;
    section_approval: string;
    section_approval_help: string;
    approval_hint: string;
    section_dynamic_workers: string;
    section_dynamic_workers_help: string;
    dynamic_workers_hint: string;
    approval_timeout: string;
    approval_timeout_brief: string;
    approval_timeout_help: string;
    section_knowledge: string;
    section_knowledge_help: string;
    knowledge_hint: string;
    knowledge_placeholder: string;
    knowledge_chunk_label: string;
    section_skills: string;
    section_skills_help: string;
    skills_hint: string;
    auto_attach_evolved: string;
    auto_attach_evolved_help: string;
    skills_search: string;
    skills_empty: string;
    skills_filter_category: string;
    skills_filter_source: string;
    skills_source_platform: string;
    skills_source_tenant: string;
    skills_tier_locked: string;
    skills_tier_locked_hint: string;
    section_subagents: string;
    section_subagents_help: string;
    subagents_hint: string;
    subagent_name: string;
    subagent_ref: string;
    subagent_description: string;
    subagent_remove: string;
    subagent_add: string;
    section_prompt_vars: string;
    section_prompt_vars_help: string;
    prompt_jinja_label: string;
    prompt_jinja_hint: string;
    prompt_var_name: string;
    prompt_var_trusted: string;
    prompt_var_required: string;
    prompt_var_description: string;
    prompt_var_remove: string;
    prompt_var_add: string;
    section_output_schema: string;
    output_schema: {
      on_label: string;
      hint_off: string;
      col_name: string;
      col_type: string;
      col_required: string;
      col_desc: string;
      type_string: string;
      type_number: string;
      type_integer: string;
      type_boolean: string;
      type_array_string: string;
      type_array_number: string;
      add_field: string;
      remove_field: string;
      name_invalid: string;
      name_duplicate: string;
      complex_readonly: string;
      off_confirm: string;
    };
    inject_date_label: string;
    inject_date_hint: string;
    dynamic_context_note: string;
  };
  playground: {
    session_label: string;
    new_session: string;
    session_failed: string;
    thread_id: string;
    input_placeholder: string;
    run: string;
    running: string;
    stop: string;
    event_log: string;
    event_count: string;
    stream_failed: string;
    empty_log: string;
    attach_image: string;
    attach_document: string;
    uploaded_docs_note: string;
    prompt_vars_label: string;
    attachments_label: string;
    remove_attachment: string;
    upload_failed: string;
    workspace_full: string;
    uploading: string;
    transcript_label: string;
    turn_count: string;
    turn_running: string;
    turn_no_text: string;
    segment_commentary: string;
    turn_failed: string;
    usage_in: string;
    usage_out: string;
    usage_total: string;
    usage_cache: string;
    usage_reasoning: string;
    usage_cache_write: string;
    meta_finish: string;
    meta_model: string;
    reasoning_label: string;
    view_full_text: string;
    copy_all: string;
    copy_done: string;
    copy_failed: string;
    events_label: string;
    export_json: string;
    export_json_tip: string;
    retry: string;
    workspace_label: string;
    workspace_refresh: string;
    workspace_volume: string;
    workspace_size: string;
    workspace_deleted: string;
    workspace_none: string;
    workspace_artifacts: string;
    workspace_files: string;
    workspace_file_download: string;
    artifact_download: string;
    artifact_delete: string;
    artifact_delete_confirm: string;
    file_delete: string;
    file_delete_confirm: string;
    delete_ok: string;
    delete_cancel: string;
    resume_label: string;
    resumed_notice: string;
    history_divider: string;
    history_loading: string;
    meta_steps: string;
    meta_latency: string;
    view_run: string;
    feedback_up: string;
    feedback_down: string;
    feedback_comment_placeholder: string;
    feedback_comment_submit: string;
    feedback_thanks: string;
    feedback_failed: string;
    approval_awaiting: string;
    approval_approve: string;
    approval_reject: string;
    approval_modify_hint: string;
    history_button: string;
    tool_count_one: string;
    tool_count_other: string;
    tool_failed_count: string;
    state_memories: string;
    state_failures: string;
    state_reflections: string;
    state_subagents: string;
    state_signals: string;
    state_retries: string;
    state_per_step: string;
    signal_no_progress: string;
    signal_escalate: string;
    retry_attempt: string;
    state_section: string;
    tl_step: string;
    streaming_badge: string;
    interrupted_badge: string;
    ttft: string;
    streaming_reasoning_label: string;
    reasoning_summary: string;
    tl_finish: string;
    tl_tool_failed: string;
    tl_final_answer: string;
    tl_step_output: string;
    tl_reasoning: string;
    tl_expand: string;
    tl_critique: string;
    tl_legend_agent: string;
    tl_legend_mem: string;
    tl_legend_plan: string;
    tl_legend_reflect: string;
    tl_legend_marker: string;
    tl_legend_approval: string;
    tl_importance: string;
    tl_confidence: string;
    tl_goal: string;
    tl_duration: string;
    tr_col_action: string;
    tr_state_not_ready_title: string;
    tr_state_not_ready_msg: string;
    tr_state_unavailable_title: string;
    tr_state_unavailable_msg: string;
    tr_state_no_trace_title: string;
    tr_state_no_trace_msg: string;
    tr_detail_close: string;
    rb_ok: string;
    rb_failed_at: string;
    rb_jump: string;
    tr_detail_latency: string;
    tr_detail_model: string;
    tr_detail_tokens: string;
    tr_detail_cost: string;
    tr_io_llm_msgs: string;
    tr_io_llm_msgs_hint: string;
    tr_io_llm_out: string;
    tr_io_llm_out_hint: string;
    tr_io_tool_args: string;
    tr_io_tool_args_hint: string;
    tr_io_tool_result: string;
    tr_io_tool_result_hint: string;
    tr_io_in: string;
    tr_io_out: string;
    tr_msg_truncated: string;
    tr_msg_copy: string;
    tr_msg_raw: string;
    tr_msg_untrusted: string;
    tr_msg_toolcall: string;
    tr_msg_chars: string;
    tr_purpose_primary: string;
    tr_aux_llm: string;
    tr_raw_error: string;
    tl_worker_running: string;
    tl_worker_success: string;
    tl_worker_max_steps: string;
    tl_worker_cancelled: string;
    tl_worker_task: string;
    tl_worker_summary: string; // "{{steps}} steps · {{calls}} LLM calls · {{duration}}"
    tl_worker_children: string;
    task_result: string;
    lifecycle_created: string;
    lifecycle_fired: string;
    lifecycle_completed: string;
    fire_pending: string;
    gantt_expand: string;
    gantt_degraded: string;
  };
  session_history: {
    title: string;
    search_placeholder: string;
    filter_status: string;
    filter_all: string;
    empty: string;
    empty_search: string;
    load_more: string;
    rename: string;
    rename_placeholder: string;
    rename_ok_button: string;
    rename_ok: string;
    archive: string;
    archive_confirm: string;
    archive_ok: string;
    purge: string;
    purge_confirm: string;
    purge_warning: string;
    purge_ok: string;
    cancel: string;
    action_failed: string;
    time_now: string;
    time_minutes: string;
    time_hours: string;
    time_days: string;
    status_active: string;
    status_paused: string;
    status_completed: string;
    status_failed: string;
    status_cancelled: string;
    status_archived: string;
  };
  event_stream: {
    title: string;
    connecting: string;
    event_count: string;
    stream_failed: string;
    empty: string;
    view_timeline: string;
    view_raw: string;
    view_exact: string;
    compaction_label: string;
    compaction_passes: string;
    compaction_reduction: string;
    compaction_summary_chars: string;
  };
  tool_timeline: {
    empty: string;
    mcp_badge: string;
    builtin_badge: string;
    status_pending: string;
    status_success: string;
    status_error: string;
    status_pending_approval: string;
    args_label: string;
    result_label: string;
    exit_code: string;
    stdout_label: string;
    stderr_label: string;
    timed_out: string;
    fire_now: string;
    firing: string;
    fire_delivered: string;
    fire_pending: string;
    fire_failed: string;
  };
  approval_card: {
    awaiting_human: string;
    reason_kind: string;
    requested_at: string;
    timeout_at: string;
    proposed_args_label: string;
    editing_hint: string;
    edit_arguments: string;
    cancel_edit: string;
    approve: string;
    approve_with_edits: string;
    reject: string;
    approved: string;
    approved_with_edits: string;
    rejected: string;
    json_parse_error: string;
    json_must_be_object: string;
    binding: string;
    binding_hint: string;
    workspace_drift_title: string;
    workspace_drift_body: string;
  };
  plan_panel: {
    title: string;
    progress: string;
    edit: string;
    cancel: string;
    save: string;
    saved: string;
    locked_while_running: string;
    goal_placeholder: string;
    step_placeholder: string;
    add_step: string;
    remove_step: string;
    no_plan: string;
    status_pending: string;
    status_in_progress: string;
    status_completed: string;
  };
  run_detail: {
    failed_to_load: string;
    back_to_conversation: string;
    thread_label: string;
    awaiting_approval: string;
    reason_kind: string;
    requested_at: string;
    timeout_at: string;
    proposed_args: string;
    approve: string;
    reject: string;
    approved: string;
    rejected: string;
    run_metadata: string;
    run_id: string;
    thread_id: string;
    status: string;
  };
  trace_toolbar: {
    title: string;
    no_trace: string;
    copy_aria: string;
    copied: string;
    open_in_langfuse: string;
    langfuse_unconfigured_hint: string;
  };
  trace: {
    breakdown_title: string;
  };
  approval_badge: {
    aria_label: string;
    tooltip_one: string;
    tooltip_other: string;
  };
  curation: {
    page_title: string;
    subtitle: string;
    tab_candidates: string;
    tab_datasets: string;
    cross_tenant_banner: string;
    failed_to_load: string;
    empty_home: string;
    empty_cross: string;
    col_signal: string;
    col_agent: string;
    col_status: string;
    col_detected: string;
    col_outcome: string;
    filter_status: string;
    filter_status_all: string;
    filter_signal: string;
    filter_signal_all: string;
    detail_title: string;
    detail_signal: string;
    detail_outcome: string;
    detail_trajectory: string;
    trajectory_missing: string;
    promote: string;
    dismiss: string;
    promote_modal_title: string;
    promote_dataset_name: string;
    promote_name_required: string;
    promote_hint: string;
    promoted: string;
    dismissed: string;
  };
  audit: {
    page_title: string;
    subtitle: string;
    cross_tenant_banner: string;
    failed_to_load: string;
    empty: string;
    filter_actor: string;
    filter_action: string;
    filter_resource_type: string;
    filter_result: string;
    filter_result_all: string;
    load_more: string;
    detail_title: string;
    detail_id: string;
    detail_occurred_at: string;
    detail_actor: string;
    detail_on_behalf_of: string;
    detail_action: string;
    detail_resource: string;
    detail_result: string;
    detail_reason: string;
    detail_trace_id: string;
    detail_ip: string;
    detail_payload: string;
    detail_payload_hint: string;
    mcp_traffic_hint: string;
  };
  egress_audit: {
    page_title: string;
    subtitle: string;
    cross_tenant_banner: string;
    load_error: string;
    empty: string;
    filter_verdict: string;
    filter_host: string;
    filter_agent: string;
    load_more: string;
    detail_title: string;
    col_verdict: string;
    col_host: string;
    col_agent: string;
    col_bytes_up: string;
    col_bytes_down: string;
    col_duration: string;
    col_sandbox: string;
    col_error: string;
    col_time: string;
  };
  settings_ops: {
    quotas_page_title: string;
    quotas_subtitle: string;
    config_page_title: string;
    config_subtitle: string;
    config_record_title: string;
    config_edit_title: string;
    config_not_found: string;
    cross_tenant_blocked_title: string;
    cross_tenant_blocked_body: string;
    failed_to_load: string;
    quota_create: string;
    quota_create_modal_title: string;
    quota_created: string;
    quota_deleted: string;
    quota_empty: string;
    quota_delete_confirm_title: string;
    quota_delete_confirm_body: string;
    col_dimension: string;
    col_limit_value: string;
    col_burst: string;
    col_scope: string;
    col_updated: string;
    col_actions: string;
    field_dimension: string;
    field_limit_value: string;
    field_burst: string;
    burst_hint: string;
    dimension_required: string;
    limit_required: string;
    display_name: string;
    plan: string;
    audit_retention_days: string;
    event_log_retention_days: string;
    // Stream C.6 — per-tenant rate limit override.
    rate_limit_override: string;
    rate_limit_default: string;
    // Sprint #4 (Mini-ADR U-28) — Curator thresholds.
    skill_stale_days: string;
    skill_archive_days: string;
    skill_evolution_enabled: string;
    skill_evolution_on: string;
    skill_evolution_off: string;
    skill_evolution_judge_sample_pct: string;
    mcp_allowlist: string;
    http_allowlist: string;
    http_allowlist_empty: string;
    http_denylist: string;
    http_denylist_empty: string;
    updated: string;
    dirty: string;
    config_saved: string;
    config_parse_error: string;
    config_etag_hint_title: string;
    config_etag_hint_body: string;
  };
  settings_credentials: {
    page_title: string;
    subtitle: string;
    failed_to_load: string;
    mode_label: string;
    mode_platform: string;
    mode_help_platform: string;
    providers_heading: string;
    tools_heading: string;
    col_provider: string;
    col_tool: string;
    col_platform_status: string;
    col_used_by: string;
    status_configured: string;
    status_not_set: string;
    empty: string;
  };
  settings_tenants: {
    page_title: string;
    subtitle: string;
    not_admin_title: string;
    not_admin_body: string;
    col_display_name: string;
    col_plan: string;
    col_tenant_id: string;
    col_created: string;
    col_actions: string;
    col_status: string;
    st_active: string;
    st_suspended: string;
    deactivate: string;
    activate: string;
    deactivate_confirm: string;
    status_change_failed: string;
    status_changed: string;
    manage: string;
    failed_to_load: string;
    empty: string;
    create: string;
  };
  settings_create_tenant: {
    page_title: string;
    subtitle: string;
    not_admin_title: string;
    not_admin_body: string;
    field_display_name: string;
    display_name_required: string;
    field_plan: string;
    field_tenant_id: string;
    tenant_id_hint: string;
    tenant_id_placeholder: string;
    tenant_id_invalid: string;
    create_btn: string;
    created: string;
    created_detail: string;
    field_first_admin_email: string;
    field_first_admin_display_name: string;
    first_admin_hint: string;
    first_admin_email_invalid: string;
    first_admin_provisioned: string;
  };
  mcp_servers: {
    page_title: string;
    subtitle: string;
    add: string;
    col_name: string;
    col_transport: string;
    col_url: string;
    col_auth: string;
    col_status: string;
    col_tools: string;
    col_tenant: string;
    col_actions: string;
    status_enabled: string;
    status_disabled: string;
    test: string;
    edit: string;
    delete: string;
    testing: string;
    connected: string;
    unreachable: string;
    tools_loading: string;
    no_tools: string;
    empty_title: string;
    empty_hint: string;
    delete_confirm: string;
    failed_to_load: string;
    source_platform: string;
    source_custom: string;
    platform_hosted: string;
    status_enabled_platform: string;
    needs_authorize: string;
    remove: string;
    remove_confirm: string;
    remove_unavailable: string;
    act_run: string;
    act_stop: string;
  };
  create_mcp_server: {
    add_title: string;
    edit_title: string;
    field_name: string;
    field_transport: string;
    field_url: string;
    field_auth: string;
    field_token: string;
    field_timeout: string;
    token_hint_create: string;
    token_hint_edit: string;
    test_connection: string;
    test_ok: string;
    test_failed: string;
    name_required: string;
    url_required: string;
    url_invalid: string;
    token_required: string;
    submit_add: string;
    submit_save: string;
    custom_disabled: string;
    tab_basic: string;
    tab_headers: string;
    tab_config: string;
    headers_hint: string;
    headers_hint_edit: string;
    header_name: string;
    header_value: string;
    header_value_keep: string;
    header_add: string;
    header_name_invalid: string;
    field_sse_read_timeout: string;
    sse_read_timeout_hint: string;
    field_name_help: string;
    field_transport_help: string;
    field_url_help: string;
    field_auth_help: string;
    field_token_help: string;
    field_timeout_help: string;
    field_sse_read_timeout_help: string;
  };
  mcp_catalog: {
    page_title: string;
    subtitle: string;
    add: string;
    not_admin_title: string;
    not_admin_body: string;
    failed_to_load: string;
    col_name: string;
    col_category: string;
    col_transport: string;
    col_tier: string;
    col_enabled: string;
    col_actions: string;
    empty_title: string;
    empty_hint: string;
    delete_confirm: string;
    delete_in_use: string;
    deleted: string;
    tier_free: string;
    tier_pro: string;
    tier_enterprise: string;
    auth_none: string;
    auth_bearer: string;
    auth_bearer_shared: string;
    auth_oauth2: string;
    tab_basic: string;
    tab_auth: string;
    tab_advanced: string;
    field_url: string;
    url_hint: string;
    field_bearer_token: string;
    bearer_token_required: string;
    bearer_token_keep_hint: string;
    shared_bearer_warning: string;
    field_oauth_client_id: string;
    oauth_client_id_required: string;
    field_oauth_scopes: string;
    oauth_hint: string;
    add_title: string;
    edit_title: string;
    submit_add: string;
    submit_save: string;
    field_identifier: string;
    field_name_hint: string;
    field_display_name: string;
    field_description: string;
    field_category: string;
    category_placeholder: string;
    cat_search: string;
    cat_database: string;
    cat_payment: string;
    cat_location: string;
    cat_social: string;
    cat_design: string;
    cat_document: string;
    cat_browser: string;
    cat_scraping: string;
    cat_dev_tools: string;
    cat_other: string;
    field_icon: string;
    icon_hint: string;
    icon_upload: string;
    icon_clear: string;
    icon_type_error: string;
    icon_too_large: string;
    field_transport: string;
    field_auth: string;
    field_timeout: string;
    timeout_hint: string;
    field_sse_timeout: string;
    sse_timeout_hint: string;
    field_required_tier: string;
    field_enabled: string;
    name_required: string;
    display_name_required: string;
    url_template_required: string;
    browser_title: string;
    browser_empty: string;
    browser_failed: string;
    locked_ribbon: string;
    requires_tier: string;
    advanced_hint: string;
    advanced_custom: string;
    back: string;
    err_tier_required: string;
    err_not_found: string;
    oauth_badge: string;
    enable: string;
    enabled: string;
    enable_aria: string;
    authorize: string;
    shared_hint: string;
    tools_title: string;
    tools_none: string;
    tools_unreachable: string;
    tools_oauth_note: string;
    tab_config: string;
    tools_refresh: string;
    param_name: string;
    param_type: string;
    param_desc: string;
    tool_enable_aria: string;
    tool_no_params: string;
  };
  mcp_oauth: {
    page_title: string;
    page_subtitle: string;
    failed_to_load: string;
    empty: string;
    col_connector: string;
    col_status: string;
    col_scopes: string;
    col_expires: string;
    col_actions: string;
    status_pending: string;
    status_connected: string;
    status_expired: string;
    status_revoked: string;
    status_error: string;
    has_error: string;
    reauthorize: string;
    reauthorize_failed: string;
    disconnect: string;
    disconnect_confirm: string;
    disconnect_failed: string;
    disconnected: string;
    connect_title: string;
    connect_hint: string;
    authorize: string;
    err_not_oauth: string;
    err_not_configured: string;
    callback_title: string;
    callback_exchanging: string;
    callback_failed: string;
    callback_back: string;
    callback_missing_params: string;
  };
  settings_platform: {
    page_title: string;
    subtitle: string;
    not_admin_title: string;
    not_admin_body: string;
    failed_to_load: string;
    tab_credentials: string;
    tab_models: string;
    tab_cost: string;
    tab_quality: string;
    providers_heading: string;
    tools_heading: string;
    col_name: string;
    col_source: string;
    col_secret_ref: string;
    col_enabled: string;
    col_used_by: string;
    col_actions: string;
    source_env: string;
    source_db: string;
    source_unset: string;
    edit_btn: string;
    delete_confirm: string;
    edit_modal_title: string;
    mode_label: string;
    mode_value: string;
    mode_ref: string;
    value_label: string;
    value_hint: string;
    value_required: string;
    secret_ref_label: string;
    secret_ref_hint: string;
    enabled_label: string;
    saved: string;
    deleted: string;
    unset_ref: string;
    col_keys: string;
    col_key_id: string;
    col_priority: string;
    keys_summary: string;
    add_key_btn: string;
    add_key_modal_title: string;
    key_id_label: string;
    key_id_hint: string;
    key_id_invalid: string;
    priority_label: string;
    priority_hint: string;
    delete_key_confirm: string;
    col_tenant_overrides: string;
    tenant_overrides_btn: string;
    tenant_drawer_title: string;
    tenant_drawer_hint: string;
    tenant_select_label: string;
    tenant_select_placeholder: string;
    col_effective: string;
    tenant_source_tenant: string;
    tenant_source_suppressed: string;
    tenant_source_db: string;
    tenant_source_env: string;
    tenant_source_unset: string;
    override_btn: string;
    delete_override_confirm: string;
    edit_override_modal_title: string;
    embedding_heading: string;
    embedding_current: string;
    embedding_unconfigured: string;
    embedding_provider_label: string;
    embedding_model_label: string;
    rerank_enable: string;
    rerank_provider_label: string;
    rerank_model_label: string;
    embedding_save: string;
    embedding_saved: string;
    embedding_err_EMBEDDING_PROVIDER_KEY_MISSING: string;
    embedding_err_INVALID_EMBEDDING_MODEL: string;
    embedding_err_INVALID_RERANK_PAIR: string;
    embedding_err_RERANK_PROVIDER_KEY_MISSING: string;
    embedding_err_INVALID_RERANK_MODEL: string;
    judge_heading: string;
    judge_help_title: string;
    judge_help_body: string;
    judge_current: string;
    judge_unconfigured: string;
    judge_provider_label: string;
    judge_model_label: string;
    judge_save: string;
    judge_saved: string;
    judge_clear: string;
    judge_err_INVALID_JUDGE_PAIR: string;
    judge_err_JUDGE_PROVIDER_KEY_MISSING: string;
    judge_err_INVALID_JUDGE_MODEL: string;
    billing_heading: string;
    billing_help_title: string;
    billing_help_body: string;
    billing_title: string;
    billing_enabled: string;
    billing_disabled: string;
    billing_hint: string;
    billing_saved: string;
    billing_save_failed: string;
    tool_budget_heading: string;
    tool_budget_help_title: string;
    tool_budget_help_body: string;
    tool_budget_title: string;
    tool_budget_enabled: string;
    tool_budget_disabled: string;
    tool_budget_hint: string;
    tool_budget_env_default: string;
    tool_budget_toggle_label: string;
    tool_budget_saved: string;
    tool_budget_save_failed: string;
    dynamic_worker_heading: string;
    dynamic_worker_help_title: string;
    dynamic_worker_help_body: string;
    dynamic_worker_max_concurrent_label: string;
    dynamic_worker_max_per_run_label: string;
    dynamic_worker_max_iterations_label: string;
    dynamic_worker_env_default: string;
    dynamic_worker_hint: string;
    dynamic_worker_save: string;
    dynamic_worker_saved: string;
    dynamic_worker_save_failed: string;
    delegation_heading: string;
    delegation_help_title: string;
    delegation_help_body: string;
    delegation_max_concurrent_delegations_label: string;
    delegation_env_default: string;
    delegation_hint: string;
    delegation_save: string;
    delegation_saved: string;
    delegation_save_failed: string;
    quality_heading: string;
    quality_help_title: string;
    quality_help_body: string;
    quality_default_note: string;
    quality_deploy_note: string;
    quality_section_master: string;
    quality_enabled_label: string;
    quality_enabled_hint: string;
    quality_section_sampling: string;
    quality_sampling_rate_label: string;
    quality_daily_cap_label: string;
    quality_monitor_interval_label: string;
    quality_batch_size_label: string;
    quality_section_judge: string;
    quality_judge_provider_label: string;
    quality_judge_model_label: string;
    quality_judge_hint: string;
    quality_section_drift: string;
    quality_drift_interval_label: string;
    quality_recent_window_label: string;
    quality_baseline_window_label: string;
    quality_min_samples_label: string;
    quality_threshold_label: string;
    quality_cooldown_label: string;
    quality_save: string;
    quality_saved: string;
    quality_save_error: string;
    quality_err_JUDGE_PROVIDER_KEY_MISSING: string;
    quality_err_INVALID_JUDGE_MODEL: string;
  };
  settings_iam: {
    sa_page_title: string;
    sa_subtitle: string;
    sa_api_keys_hint: string;
    sa_create: string;
    sa_create_modal_title: string;
    sa_created: string;
    sa_deleted: string;
    sa_empty_home: string;
    sa_empty_cross: string;
    sa_delete_confirm_title: string;
    sa_delete_confirm_body: string;
    rb_page_title: string;
    rb_subtitle: string;
    rb_create: string;
    rb_create_drawer_title: string;
    rb_create_submit: string;
    rb_create_platform_submit: string;
    rb_filter_platform_scope: string;
    rb_created: string;
    rb_deleted: string;
    rb_empty_home: string;
    rb_empty_cross: string;
    rb_empty_platform: string;
    rb_delete_confirm_title: string;
    rb_delete_confirm_body: string;
    rb_delete_platform_warn: string;
    rb_confirm_required: string;
    cross_tenant_banner: string;
    failed_to_load: string;
    col_name: string;
    col_description: string;
    col_active: string;
    col_created: string;
    col_subject_type: string;
    col_subject_id: string;
    col_role: string;
    col_tenant: string;
    col_conditions: string;
    col_granted_at: string;
    col_actions: string;
    conditions_none: string;
    section_conditions: string;
    conditions_hint: string;
    field_resource_ids: string;
    resource_ids_hint: string;
    resource_ids_placeholder: string;
    field_labels: string;
    labels_hint: string;
    field_owner_only: string;
    field_name: string;
    field_description: string;
    field_subject_type: string;
    field_subject_id: string;
    field_role: string;
    field_platform_scope: string;
    platform_scope_hint: string;
    platform_scope_warn_title: string;
    platform_scope_warn_body: string;
    role_requires_platform_scope: string;
    confirm_phrase_label: string;
    name_required: string;
    subject_id_required: string;
    role_required: string;
  };
  settings_members: {
    page_title: string;
    subtitle: string;
    invite: string;
    filter_all: string;
    col_email: string;
    col_name: string;
    col_role: string;
    col_status: string;
    col_tenant: string;
    col_invited_at: string;
    col_actions: string;
    cross_tenant_banner: string;
    status_invited: string;
    status_active: string;
    status_suspended: string;
    status_revoked: string;
    resend: string;
    remove: string;
    set_password: string;
    set_password_title: string;
    set_password_hint: string;
    set_password_label: string;
    set_password_placeholder: string;
    set_password_submit: string;
    set_password_ok: string;
    set_password_failed: string;
    set_password_too_short: string;
    revoke_confirm_title: string;
    revoke_confirm_body: string;
    suspend_confirm_title: string;
    suspend_confirm_body: string;
    invite_drawer_title: string;
    field_email: string;
    field_role: string;
    field_display_name: string;
    field_display_name_placeholder: string;
    email_required: string;
    email_invalid: string;
    role_required: string;
    invite_submit: string;
    invite_success: string;
    invite_partial_fail: string;
    resent: string;
    removed: string;
    failed_to_load: string;
    empty: string;
    empty_cross: string;
    purge_action: string;
    purge_confirm_title: string;
    purge_confirm_body: string;
    purge_type_to_confirm: string;
    purge_no_data_note: string;
    purge_done: string;
    purge_partial: string;
  };
  settings_platform_users: {
    page_title: string;
    subtitle: string;
    not_admin_title: string;
    not_admin_body: string;
    failed_to_load: string;
    col_subject: string;
    col_role: string;
    col_granted_by: string;
    col_granted_at: string;
    col_actions: string;
    you: string;
    grant: string;
    granted: string;
    revoke: string;
    revoked: string;
    revoke_confirm: string;
    revoke_self_warning: string;
    subject_label: string;
    subject_hint: string;
    subject_invalid: string;
  };
  triggers: {
    page_title: string;
    subtitle: string;
    cross_tenant_banner: string;
    failed_to_load: string;
    empty_cron: string;
    empty_webhook: string;
    empty_cross: string;
    tab_cron: string;
    tab_webhook: string;
    col_name: string;
    col_agent: string;
    col_cron_expr: string;
    col_webhook_path: string;
    col_enabled: string;
    col_updated: string;
    col_actions: string;
    create: string;
    create_submit: string;
    create_cron_title: string;
    create_webhook_title: string;
    field_name: string;
    field_agent_name: string;
    field_agent_version: string;
    field_cron_expr: string;
    name_required: string;
    agent_required: string;
    agent_version_required: string;
    cron_required: string;
    cron_hint: string;
    webhook_secret_info_title: string;
    webhook_secret_info_body: string;
    secret_drawer_title: string;
    secret_warn_title: string;
    secret_warn_body: string;
    secret_trigger_name: string;
    secret_label: string;
    secret_copy: string;
    secret_copied: string;
    secret_copy_failed: string;
    secret_acknowledged: string;
    toggled: string;
    created: string;
    deleted: string;
    delete_confirm_title: string;
    delete_confirm_body: string;
  };
  webhooks: {
    page_title: string;
    subtitle: string;
    cross_tenant_banner: string;
    failed_to_load: string;
    empty: string;
    empty_cross: string;
    all_agents: string;
    col_name: string;
    col_url: string;
    col_events: string;
    col_agent: string;
    col_enabled: string;
    col_actions: string;
    create: string;
    create_submit: string;
    create_title: string;
    field_name: string;
    field_url: string;
    url_hint: string;
    field_events: string;
    events_placeholder: string;
    field_agent_name: string;
    agent_name_hint: string;
    field_payload_format: string;
    payload_format_hint: string;
    col_format: string;
    format_generic: string;
    format_feishu: string;
    format_dingtalk: string;
    format_wecom: string;
    name_required: string;
    url_required: string;
    events_required: string;
    secret_drawer_title: string;
    secret_warn_title: string;
    secret_warn_body: string;
    secret_endpoint_name: string;
    secret_label: string;
    secret_copy: string;
    secret_copied: string;
    secret_copy_failed: string;
    secret_acknowledged: string;
    toggled: string;
    created: string;
    deleted: string;
    delete_confirm_title: string;
    delete_confirm_body: string;
  };
  skills: {
    page_title: string;
    subtitle: string;
    cross_tenant_banner: string;
    failed_to_load: string;
    empty_home: string;
    empty_cross: string;
    col_name: string;
    col_status: string;
    revision_pending: string;
    revision_pending_tooltip: string;
    col_category: string;
    col_description: string;
    col_updated: string;
    col_created: string;
    col_version: string;
    col_tools: string;
    col_authored_by: string;
    col_actions: string;
    filter_status: string;
    filter_status_all: string;
    filter_category: string;
    load_more: string;
    create: string;
    create_modal_title: string;
    import_zip: string;
    export_zip: string;
    field_name: string;
    field_category: string;
    field_description: string;
    name_required: string;
    category_required: string;
    description_required: string;
    create_hint: string;
    created: string;
    imported: string;
    status_changed: string;
    change_status: string;
    status_draft: string;
    status_active: string;
    status_stale: string;
    status_archived: string;
    metadata_title: string;
    versions_title: string;
    no_versions: string;
    latest_version_hint: string;
    // ── Capability Uplift Sprint #3 PR C — Admin UI dual-pane editor ──
    detail_files_title: string;
    detail_editor_title: string;
    detail_no_file_selected: string;
    detail_select_file_hint: string;
    detail_skill_md_pinned: string;
    detail_supporting_files_section: string;
    detail_no_supporting_files: string;
    detail_version_picker_label: string;
    detail_active_version_marker: string;
    detail_lazy_badge: string;
    detail_lazy_tooltip: string;
    detail_eager_tooltip: string;
    detail_high_risk_badge: string;
    detail_high_risk_tooltip: string;
    detail_high_risk_warning: string;
    detail_admin_required_tooltip: string;
    detail_unsaved_changes_warning: string;
    detail_diff_toggle: string;
    detail_diff_original_label: string;
    detail_diff_draft_label: string;
    file_add: string;
    file_add_modal_title: string;
    file_add_path_label: string;
    file_add_path_placeholder: string;
    file_add_content_label: string;
    file_add_upload_label: string;
    file_add_upload_hint: string;
    file_add_submit: string;
    file_action_edit: string;
    file_action_delete: string;
    file_action_rename: string;
    file_action_save: string;
    file_action_cancel: string;
    file_save_failed: string;
    file_saved: string;
    file_deleted: string;
    file_delete_confirm_title: string;
    file_delete_confirm_body: string;
    file_delete_confirm_input_hint: string;
    file_rename_modal_title: string;
    file_rename_new_path_label: string;
    file_rename_submit: string;
    file_renamed: string;
    file_binary_placeholder: string;
    file_size_label: string;
    file_mime_label: string;
    file_load_failed: string;
    detail_skill_md_edit_hint: string;
    // Sprint #4 — Curator pin + ETA hint.
    pin: string;
    unpin: string;
    pinned_toast: string;
    unpinned_toast: string;
    pin_tooltip_on: string;
    pin_tooltip_off: string;
    eta_days_to_stale: string;
    eta_due_soon: string;
    // Stream X-6 — merged tenant/platform library badges.
    source_platform: string;
    source_tenant: string;
    requires_tier: string;
    requires_tier_tooltip: string;
  };
  agent_template_marketplace: {
    page_title: string;
    subtitle: string;
    failed_to_load: string;
    empty: string;
    tier_free: string;
    tier_pro: string;
    tier_enterprise: string;
    requires_tier: string;
    locked_ribbon: string;
    fork: string;
    fork_title: string;
    fork_name_label: string;
    fork_name_hint: string;
    fork_name_required: string;
    fork_name_invalid: string;
    forked_ok: string;
  };
  agent_templates: {
    page_title: string;
    subtitle: string;
    add: string;
    deleted: string;
    delete_confirm: string;
    not_admin_title: string;
    not_admin_body: string;
    failed_to_load: string;
    empty_title: string;
    empty_hint: string;
    col_name: string;
    col_category: string;
    col_tier: string;
    col_status: string;
    col_enabled: string;
    col_actions: string;
    tier_free: string;
    tier_pro: string;
    tier_enterprise: string;
    status_draft: string;
    status_published: string;
    create_title: string;
    create_btn: string;
    invalid_manifest: string;
    save_failed: string;
    saved_ok: string;
    yes: string;
    no: string;
    back: string;
    field_display_name: string;
    display_name_required: string;
    field_description: string;
    field_category: string;
    field_icon: string;
    field_tier: string;
    field_status: string;
    field_enabled: string;
    tab_basic: string;
    field_display_name_help: string;
    field_description_help: string;
    field_category_help: string;
    field_icon_help: string;
    field_tier_help: string;
    field_status_help: string;
    field_enabled_help: string;
    cat_support: string;
    cat_sales: string;
    cat_research: string;
    cat_coding: string;
    cat_data: string;
    cat_productivity: string;
    cat_general: string;
    cat_other: string;
  };
  platform_skills: {
    page_title: string;
    subtitle: string;
    add: string;
    import_zip: string;
    import_github: string;
    github_modal_title: string;
    github_hint: string;
    github_source_label: string;
    github_source_ph: string;
    github_source_help: string;
    github_skill_label: string;
    github_skill_ph: string;
    github_skill_help: string;
    github_ref_label: string;
    github_ref_ph: string;
    github_ref_help: string;
    github_submit: string;
    github_pick_skill: string;
    github_pick_ph: string;
    github_select_all: string;
    github_clear_all: string;
    github_batch_submit: string;
    github_batch_done: string;
    github_result_created: string;
    github_result_exists: string;
    github_result_failed: string;
    imported: string;
    import_noop: string;
    manage: string;
    pin: string;
    unpin: string;
    batch_toolbar_aria: string;
    batch_select_row: string;
    batch_select_all: string;
    batch_selected: string;
    batch_lock: string;
    batch_unlock: string;
    batch_archive: string;
    batch_activate: string;
    batch_clear: string;
    batch_done: string;
    batch_partial: string;
    batch_all_matching: string;
    batch_scope_all: string;
    batch_set_category: string;
    batch_clear_category: string;
    search_ph: string;
    search_aria: string;
    status_filter_aria: string;
    status_filter_all: string;
    not_admin_title: string;
    not_admin_body: string;
    failed_to_load: string;
    col_name: string;
    col_category: string;
    col_description: string;
    col_tier: string;
    col_status: string;
    col_version: string;
    col_actions: string;
    no_description: string;
    category_filter_all: string;
    category_filter_aria: string;
    category_uncategorized: string;
    empty_title: string;
    empty_hint: string;
    tier_free: string;
    tier_pro: string;
    tier_enterprise: string;
    status_draft: string;
    status_active: string;
    status_archived: string;
    detail_category_label: string;
    detail_category_placeholder: string;
    detail_category_save: string;
    detail_category_saved: string;
    detail_category_help: string;
    create_title: string;
    create_submit: string;
    field_name: string;
    field_name_hint: string;
    field_category: string;
    field_description: string;
    field_required_tier: string;
    name_required: string;
    created: string;
    duplicate_name: string;
    when_to_use_hint: string;
    manage_title: string;
    lifecycle_title: string;
    add_version_title: string;
    add_version_submit: string;
    field_prompt_fragment: string;
    field_version_description: string;
    field_tool_names: string;
    field_required_models: string;
    prompt_fragment_required: string;
    csv_hint: string;
    versions_title: string;
    no_versions: string;
    version_added: string;
    status_changed: string;
    pinned: string;
    unpinned: string;
    high_risk: string;
    lazy: string;
  };
  memory: {
    page_title: string;
    subtitle: string;
    cross_tenant_banner: string;
    failed_to_load: string;
    empty_home: string;
    empty_cross: string;
    col_kind: string;
    col_content: string;
    col_score: string;
    col_user: string;
    col_created: string;
    col_actions: string;
    importance_abbr: string;
    confidence_abbr: string;
    importance_tip: string;
    confidence_tip: string;
    filter_kind: string;
    filter_kind_all: string;
    search_placeholder: string;
    filter_user_placeholder: string;
    edit_title: string;
    correct: string;
    correct_title: string;
    corrected: string;
    edit_meta_user: string;
    edit_meta_kind: string;
    edit_content_label: string;
    save_dirty: string;
    embedder_note: string;
    embedder_unconfigured: string;
    empty_content: string;
    updated: string;
    deleted: string;
    delete_confirm_title: string;
    delete_confirm_body: string;
  };
  eval_datasets: {
    failed_to_load: string;
    empty_home: string;
    empty_cross: string;
    col_name: string;
    col_agent: string;
    col_source: string;
    col_updated: string;
    col_actions: string;
    create: string;
    create_modal_title: string;
    field_agent_name: string;
    field_name: string;
    agent_required: string;
    name_required: string;
    edit_title: string;
    edit_input_label: string;
    edit_expected_label: string;
    json_parse_error: string;
    created: string;
    updated: string;
    deleted: string;
    delete_confirm_title: string;
    delete_confirm_body: string;
  };
  api_keys: {
    page_title: string;
    subtitle: string;
    create: string;
    failed_to_load: string;
    empty: string;
    never: string;
    rotation_banner: string;
    rotation_help: string;
    col_prefix: string;
    col_scopes: string;
    col_service_account: string;
    col_status: string;
    col_last_used: string;
    col_expires: string;
    rotate: string;
    revoke: string;
    revoke_confirm: string;
    rotated: string;
    revoked: string;
    service_account_label: string;
    service_account_required: string;
    scopes_label: string;
    scopes_required: string;
    expires_label: string;
    dangerous: string;
    show_once_title: string;
    show_once_ack: string;
    show_once_help_prefix: string;
    show_once_help_emphasis: string;
    show_once_prefix_note: string;
    copy: string;
    copied: string;
  };
  tenant: {
    home_label_prefix: string;
    home_tenant: string;
    all_tenants: string;
    your_tenant: string;
    system_admin_hint: string;
    cross_tag: string;
  };
  agents_page: {
    page_title: string;
    cross_tenant_banner: string;
    failed_to_load: string;
    empty_home: string;
    empty_cross: string;
    column_name: string;
    column_status: string;
    column_owner: string;
    column_tenant: string;
    column_created: string;
    column_actions: string;
    status_active: string;
    status_draft: string;
    status_archived: string;
    status_deleted: string;
    search_placeholder: string;
    filter_status: string;
    filter_status_default: string;
    filter_status_all: string;
    action_playground: string;
    action_edit: string;
    action_runs: string;
    action_delete: string;
    action_disable: string;
    action_enable: string;
    delete_title: string;
    delete_warning: string;
    delete_warning_desc: string;
    delete_type_to_confirm: string;
    delete_success: string;
    delete_failed: string;
    disable_confirm_title: string;
    disable_confirm_body: string;
    disable_success: string;
    disable_failed: string;
    enable_success: string;
    enable_failed: string;
    create: string;
    from_template: string;
  };
  create_agent: {
    title: string;
    hint: string;
    submit: string;
    cancel: string;
    create_failed: string;
    embedding_required_title: string;
    embedding_required_desc: string;
    embedding_required_cta: string;
  };
  approvals_page: {
    page_title: string;
    cross_tenant_banner: string;
    filter_status: string;
    failed_to_load: string;
    empty: string;
    column_reason: string;
    column_action: string;
    column_waiting: string;
    column_timeout: string;
    column_status: string;
    column_actions: string;
    approve: string;
    reject: string;
    confirm_approve: string;
    confirm_reject: string;
    batch_approve: string;
    batch_reject: string;
    confirm_batch_approve: string;
    confirm_batch_reject: string;
    selected_count: string;
    decide_ok: string;
    decide_partial: string;
    decide_failed: string;
    select_row: string;
    select_all: string;
    waiting_minutes: string;
    waiting_hours: string;
    time_left_hours: string;
    time_left_minutes: string;
    timed_out: string;
    approve_all_page: string;
    confirm_approve_all: string;
    modify_hint: string;
  };
  runs_page: {
    column_run_id: string;
    column_status: string;
    column_duration: string;
    column_tokens: string;
    tokens_tip: string;
    duration_running: string;
    summary_title: string;
    summary_duration: string;
    summary_tokens: string;
    summary_input: string;
    summary_output: string;
    summary_cache: string;
    summary_llm_calls: string;
    summary_models: string;
    summary_resume: string;
    summary_no_tokens: string;
  };
  eval_runs_page: {
    page_title: string;
    subtitle: string;
    failed_to_load: string;
    empty: string;
    empty_cross: string;
    col_tenant: string;
    column_run_id: string;
    column_status: string;
    column_suite: string;
    column_summary: string;
    column_created: string;
    filter_status: string;
    filter_status_all: string;
    suite_label: string;
    enqueue: string;
    enqueue_success: string;
  };
  eval_run_detail: {
    failed_to_load: string;
    not_found: string;
    run_metadata: string;
    run_id: string;
    suite: string;
    status: string;
    triggered_by: string;
    created_at: string;
    started_at: string;
    finished_at: string;
    summary: string;
    cases_title: string;
    cases_empty: string;
    col_case_id: string;
    col_capability: string;
    col_result: string;
    col_scores: string;
    result_passed: string;
    result_failed: string;
  };
  usage: {
    page_title: string;
    subtitle: string;
    subtitle_cross: string;
    group_by_agent: string;
    group_by_model: string;
    col_tenant: string;
    total_billed: string;
    as_of_note: string;
    col_key: string;
    col_input_tokens: string;
    col_output_tokens: string;
    col_cache_creation_tokens: string;
    col_cache_read_tokens: string;
    col_hit_rate: string;
    col_billed: string;
    unpriced: string;
    tokens_heading: string;
    kind_heading: string;
    col_kind: string;
    kind_conversation: string;
    kind_skill_evolution: string;
    kind_memory_consolidation: string;
    realtime: string;
    realtime_note: string;
    empty: string;
    failed_to_load: string;
  };
  chargeback: {
    page_title: string;
    subtitle: string;
    not_admin_title: string;
    not_admin_body: string;
    tenant_filter: string;
    as_of: string;
    total_base: string;
    total_billed: string;
    total_margin: string;
    col_tenant: string;
    col_agent: string;
    col_input_tokens: string;
    col_output_tokens: string;
    col_cache_read_tokens: string;
    col_cache_creation_tokens: string;
    col_hit_rate: string;
    col_base: string;
    col_markup: string;
    col_billed: string;
    col_margin: string;
    col_unpriced: string;
    empty: string;
    agents_empty: string;
    failed_to_load: string;
  };
  coming_soon: {
    title_prefix: string;
    body: string;
    other_pages_prefix: string;
    other_pages_suffix: string;
  };
  cmdk: {
    placeholder: string;
    aria_label: string;
    no_matches: string;
    group_agents: string;
    group_jump: string;
    group_action: string;
    label_conversations: string;
    label_approvals: string;
    label_curation: string;
    label_eval: string;
    label_memory: string;
    label_knowledge: string;
    label_skills: string;
    label_agent_template_marketplace: string;
    label_triggers: string;
    label_webhooks: string;
    label_settings_api_keys: string;
    label_settings_platform_users: string;
    action_create_agent: string;
    action_create_api_key: string;
    action_open_settings: string;
    hint_select: string;
    hint_jump: string;
    hint_close: string;
    hint_shortcuts: string;
  };
  skill_evolution: {
    governance_title: string;
    visibility_agent_private: string;
    visibility_tenant: string;
    owner: string;
    forked_from: string;
    propose_to_tenant: string;
    pending_tenant_promotion: string;
    approve: string;
    reject: string;
    proposed_toast: string;
    approved_toast: string;
    rejected_toast: string;
    no_version_to_propose: string;
    filter_visibility: string;
    filter_visibility_all: string;
    eval_title: string;
    eval_empty: string;
    eval_n_cases: string;
    eval_aria: string;
    eval_baseline: string;
    eval_with_skill: string;
    verdict_pass: string;
    verdict_fail: string;
    verdict_inconclusive: string;
    lineage_title: string;
    lineage_versions: string;
    lineage_fork_aria: string;
    origin_human: string;
    origin_in_session: string;
    origin_distilled: string;
    kill_switch_engaged_toast: string;
    kill_switch_released_toast: string;
    kill_switch_confirm_title: string;
    kill_switch_confirm_body: string;
    kill_switch_engage: string;
    kill_switch_hint: string;
    kill_switch_halted: string;
    kill_switch_active: string;
    kill_switch_tenant_label: string;
    kill_switch_global_label: string;
    kill_switch_tenant_aria: string;
    kill_switch_global_aria: string;
  };
}

const en: TranslationKeys = {
  common: {
    sign_in: "Sign in",
    sign_out: "Sign out",
    refresh: "Refresh",
    loading: "Loading…",
    cancel: "Cancel",
    confirm: "Confirm",
    save: "Save",
    delete: "Delete",
    edit: "Edit",
    search_or_jump: "Search or jump",
    notifications: "Notifications",
    user_menu: "User menu",
    anonymous: "anonymous",
    field_help: "Field help",
    copy: "Copy",
    copied: "Copied",
    tenant_switched_readonly:
      "Viewing another tenant (read-only) — switch back to your home tenant to make changes",
    cross_tenant_readonly:
      "The all-tenants view is read-only — switch into a specific tenant to make changes",
  },
  theme: {
    switch_to_light: "Switch to Light",
    switch_to_dark: "Switch to Dark",
    toggle: "Toggle theme",
  },
  nav: {
    settings_group: "Settings",
    group_workspace: "Workspace",
    group_tenant_settings: "Tenant settings",
    group_platform: "Platform",
    members_all_tenants: "Members (all tenants)",
    agents: "Agents",
    conversations: "Conversations",
    users: "Users",
    approvals: "Approvals",
    curation: "Curation+Eval",
    eval: "Eval",
    memory: "Memory governance",
    knowledge: "Knowledge",
    skills: "Skills",
    agent_template_marketplace: "Template Marketplace",
    triggers: "Triggers",
    webhooks: "Webhooks",
    tenants: "Tenants",
    platform_credentials: "Platform Credentials",
    platform_users: "Platform Admins",
    api_keys: "API Keys",
    credentials: "Credentials",
    service_accounts: "Service Accounts",
    members: "Members",
    audit: "Audit",
    egress_audit: "Egress audit",
    mcp_servers: "MCP Servers",
    mcp_oauth: "My MCP Authorizations",
    mcp_catalog: "MCP Catalog",
    agent_templates: "Agent Templates",
    platform_skills: "Platform Skills",
    usage: "Usage",
    chargeback: "Chargeback",
    rate_card: "Model Pricing",
    observability: "Observability",
    keycloak: "Keycloak",
    quality: "Quality",
  },
  quality_page: {
    page_title: "Quality",
    subtitle: "Production-quality monitoring — sampled runs, LLM-judged, drift alerts.",
    honest_note:
      "Scores are a subjective LLM-judge rubric (1-5), not ground truth — there is no gold label. Use them as a relative signal, not a correctness oracle.",
    filter_agent: "Filter by agent",
    filter_window: "Time window",
    window_24h: "24h",
    window_7d: "7d",
    window_30d: "30d",
    failed_to_load: "Failed to load quality data",
    section_drift: "Drift alerts",
    section_trend: "Per-agent trend",
    section_low_scores: "Lowest-scoring runs",
    col_agent: "Agent",
    col_tenant: "Tenant",
    col_overall: "Score",
    col_dimensions: "Dimensions",
    col_rationale: "Rationale",
    col_observed_at: "Observed at",
    col_run: "Run",
    open_run: "Open",
    trend_mean: "Mean",
    trend_samples: "Samples",
    trend_empty: "No sampled scores in this window.",
    low_empty: "No sampled scores in this window.",
    drift_empty: "No drift alerts.",
    drift_col_recent: "Recent mean",
    drift_col_baseline: "Baseline mean",
    drift_col_drift_pct: "Drop",
    drift_col_samples: "Recent / baseline n",
    drift_col_detected_at: "Detected at",
  },
  observability_page: {
    page_title: "Observability",
    subtitle: "Platform-operator entry to the self-hosted observability stack.",
    not_admin_title: "Platform operators only",
    not_admin_body:
      "These dashboards hold platform-wide data with no per-tenant isolation, so only system admins can open them.",
    tenant_isolation_note:
      "These tools span every tenant's data (no per-tenant isolation) — platform-ops only. Tenant users get the per-run detail inside Expert Work instead.",
    open: "Open",
    unconfigured: "Not configured ({{env}})",
    langfuse_name: "Langfuse",
    langfuse_desc: "LLM traces — per-call prompts, tokens, cost and latency.",
    grafana_name: "Grafana",
    grafana_desc: "Metrics + logs dashboards (SLO, throughput, errors).",
    tempo_name: "Tempo",
    tempo_desc: "Distributed traces — cross-service infra spans.",
  },
  keycloak_page: {
    page_title: "Keycloak",
    subtitle: "Platform-operator entry to the self-hosted Keycloak admin console (IAM).",
    not_admin_title: "Platform operators only",
    not_admin_body:
      "Keycloak manages every tenant's users with no per-tenant isolation, so only system admins can open it.",
    console_name: "Keycloak admin console",
    console_desc:
      "Manage realm users; reset a member / first-admin password (realm expert-work → Users → Credentials).",
    open: "Open",
    unconfigured: "Not configured ({{env}})",
  },
  login: {
    title: "Expert Work Admin",
    paragraph:
      "Paste your OIDC JWT or Expert Work API key to sign in. Both are stored in this browser only; the control-plane re-verifies on every request.",
    token_label: "Token",
    token_placeholder: "eyJ… (JWT)   or   aforge_pat_… (Expert Work API key)",
    token_required: "Token is required",
    token_empty: "Token cannot be empty",
    pr2_hint: "OIDC code-flow login lands in H.1b PR 2 — see",
    sign_in_sso: "Sign in with SSO",
    sso_help:
      "You will be redirected to your organization's identity provider.",
    dev_login_toggle: "Developer login (paste token)",
    dev_login_hide: "Hide developer login",
    dev_login_section: "Developer login",
  },
  auth_callback: {
    title: "Signing in…",
    exchanging:
      "Exchanging the authorization code with your identity provider.",
    failed: "Sign-in failed",
  },
  setup: {
    title: "Set up Expert Work",
    paragraph:
      "No platform administrator exists yet. Create the first system admin to finish installing expert_work.",
    platform_name_label: "Platform name",
    admin_email_label: "Administrator email",
    admin_email_required: "Email is required",
    admin_email_invalid: "Enter a valid email address",
    admin_password_label: "Password",
    admin_password_required: "Password is required",
    admin_password_length: "Password must be 8–256 characters",
    admin_password_confirm_label: "Confirm password",
    admin_password_confirm_required: "Please confirm the password",
    admin_password_mismatch: "The two passwords do not match",
    admin_display_name_label: "Display name (optional)",
    setup_token_label: "Setup token",
    setup_token_hint:
      "The EXPERT_WORK_SETUP_TOKEN value configured at deploy time.",
    setup_token_required: "Setup token is required",
    notice:
      "This one-time wizard creates the first platform system administrator. It is disabled once setup completes.",
    submit: "Create administrator",
    success_toast: "Platform administrator created",
    done_title: "Platform administrator created",
    done_subtitle:
      "Expert Work is initialized. Sign in with the administrator account you just created.",
    go_to_login: "Go to sign in",
    already_initialized_title: "Already initialized",
    already_initialized_subtitle:
      "A platform administrator already exists. Sign in instead.",
    error_already_initialized:
      "The platform is already initialized — please sign in.",
    error_invalid_token: "The setup token is incorrect.",
    error_not_configured:
      "Setup is not configured on this deployment (no setup token set).",
    error_email_exists: "An account with this email already exists.",
    error_keycloak_unavailable:
      "The identity provider is unavailable. Try again shortly.",
    error_generic: "Setup failed. Please check the details and try again.",
  },
  agent_detail: {
    failed_to_load: "Failed to load agent",
    tab_overview: "Overview",
    tab_manifest: "Manifest",
    tab_playground: "Playground",
    tab_users: "Users",
    tab_conversations: "Conversations",
    tab_skills: "Skills",
    tab_triggers: "Triggers",
    tab_history: "Config history",
    tab_coming_soon: 'Tab "{{tab}}" lands in Stream H.2.',
    config_summary: "Configuration",
    field_id: "Record ID",
    field_tenant: "Tenant",
    field_spec_sha: "Spec sha256",
    field_status: "Status",
    field_created: "Created",
    field_updated: "Updated",
    egress_title: "Egress policy",
    egress_policy: "Policy",
    egress_allowlist: "Host allowlist",
    egress_isolated: "isolated (no egress)",
    egress_proxied: "proxied (audited)",
    egress_allow_all: "any public host (audited)",
    disabled_tag: "Disabled",
    disabled_reason: "Reason: {{reason}}",
    disable: "Disable",
    enable: "Enable",
    disable_confirm_title: "Disable this agent?",
    disable_confirm_body:
      "Rejects new runs and sessions across all versions, and cancels its in-flight runs. Reversible.",
    disable_reason_placeholder: "Reason (optional, shown in audit)",
    enable_confirm: "Re-enable this agent? New runs resume immediately.",
    disable_ok: "Agent disabled — {{cancelled}} in-flight run(s) cancelled.",
    enable_ok: "Agent enabled.",
    disable_failed: "Failed to disable agent",
    enable_failed: "Failed to enable agent",
  },
  history_tab: {
    title: "Revision history",
    select_hint: "Select two revisions to compare",
    current: "current",
    col_revision: "Revision",
    col_sha: "Spec SHA",
    col_actor: "Actor",
    col_time: "Time",
    rollback: "Roll back",
    rollback_confirm_title: "Roll back to revision #{{revision}}?",
    rollback_confirm_body:
      "This appends a new revision with the old content — history is never rewritten.",
    diff_label: "Diff: #{{older}} → #{{newer}}",
    diff_loading: "Loading diff…",
  },
  conversations_tab: {
    title: "Conversations with this agent",
    empty: "No conversations for this agent yet.",
  },
  users_tab: {
    title: "Users of this agent",
    empty: "No user conversations for this agent yet.",
    unnamed: "Unnamed user",
    column_user: "User",
    column_conversations: "Conversations",
    column_runs: "Runs",
    column_tokens: "Tokens",
    column_last_active: "Last active",
  },
  user_detail: {
    tab_conversations: "Conversations",
    tab_memory: "Memory",
    tab_artifacts: "Artifacts",
    tab_usage: "Usage",
    conversations_empty: "No conversations with this agent.",
    memory_scope_note:
      "Long-term memory is a per-user asset shared across agents — this lists everything remembered for this user, not just this agent.",
    memory_empty: "No memories recorded for this user.",
    artifacts_scope_note:
      "Artifacts live in the user's persistent workspace shared across agents.",
    artifacts_empty: "No artifacts for this user.",
    artifact_name: "Name",
    artifact_kind: "Kind",
    artifact_version: "Latest",
    usage_scope_note: "Token usage is the user's tenant-wide month, across all agents.",
    usage_month: "Usage — {{month}}",
    usage_total: "Total tokens",
    usage_in_out: "Input / output",
    usage_by_model: "By model",
  },
  users_page: {
    page_title: "Users",
    subtitle:
      "Keyed on the user_id passed to the agent — observe interactions, manage data and assets · admin-only.",
    not_admin_title: "Admin access required",
    not_admin_body: "This page is visible to tenant admins only.",
    stat_total: "Total users",
    stat_conversations: "Total conversations",
    stat_errors: "Error conversations",
    search_placeholder: "Search user_id, name, or email…",
    type_all: "All",
    type_external: "External",
    type_member: "Member",
    truncated: "Partially truncated",
    truncated_tip: "Showing {{shown}} of {{total}} — refine or paginate to see the rest.",
    col_user: "User",
    col_type: "Type",
    col_conversations: "Conversations",
    col_runs: "Runs",
    col_errors: "Errors",
    col_last_active: "Last active",
    tag_external: "External",
    tag_member: "Member",
    failed_to_load: "Failed to load users",
    empty: "No users yet.",
  },
  user_profile: {
    back_label: "Users",
    banner:
      "Read-only page — you are viewing this user's data, not acting as them; viewing or editing someone else's data is written to the audit log.",
    tab_conversations: "Conversations",
    tab_memory: "Memory",
    tab_workspace: "Workspace",
    tab_usage: "Usage",
    subject_id_label: "Passed-in user_id",
    surrogate_label: "Internal id",
    filter_agent: "Filter by agent",
    filter_agent_all: "All agents",
    filter_range: "Active since",
    conversations_empty: "No conversations for this user.",
    memory_col_importance: "Importance",
    memory_col_confidence: "Confidence",
    memory_col_actions: "Actions",
    memory_edit: "Edit",
    memory_edit_title: "Edit memory",
    memory_edit_content: "Content",
    memory_edit_kind: "Kind",
    memory_forget: "Forget",
    memory_forget_confirm: "Forget this memory?",
    memory_updated: "Memory updated",
    memory_forgotten: "Memory forgotten",
    workspace_scope_note:
      "Workspace files and artifacts are the user's persistent assets, shared across agents.",
    workspace_volume: "Volume",
    workspace_size: "Size",
    workspace_usage: "Used {{used}} / limit {{limit}} (approx., rescanned every 30 min)",
    workspace_none: "No workspace for this user yet.",
    workspace_files: "Files",
    workspace_files_empty: "No files.",
    download: "Download",
    delete: "Delete",
    delete_confirm: "Delete {{name}}?",
    deleted: "Deleted {{name}}",
    failed_to_load: "Failed to load",
    purge_action: "Purge this user's data",
    purge_title: "Purge user data · irreversible",
    purge_warning: "This permanently purges everything for {{who}}:",
    purge_self_warning:
      "This is your own account's data — purging it deletes your own conversations, memory, and workspace, and cannot be undone.",
    purge_deletes:
      "Conversations · memory · workspace · uploads · triggers · webhooks — all deleted.",
    purge_anonymizes:
      "Billing & analytics records are kept but anonymized (the user link is nulled).",
    purge_archive_note: "The workspace is archived, then hard-deleted after 90 days.",
    purge_type_to_confirm: "Type the subject_id to confirm:",
    purge_confirm_btn: "Purge data",
    purge_done: "User data purged",
    purge_partial: "Some steps failed — you can retry (purge again).",
    purge_employee_title: "This user is an employee",
    purge_employee_body:
      "Purge employees from the Members page (which also revokes their role and Keycloak account).",
    purge_failed_title: "Purge failed",
  },
  conversations_page: {
    page_title: "Conversations",
    subtitle: "User conversations across agents, grouped by thread.",
    untitled: "Untitled conversation",
    column_conversation: "Conversation",
    column_agent: "Agent",
    column_user: "User",
    column_status: "Status",
    column_runs: "Runs",
    column_tokens: "Tokens",
    column_last_active: "Last active",
    filter_status: "Filter by status",
    filter_status_all: "All statuses",
    filter_user_tip: "Filter conversations by this user",
    filter_user_active: "User: {{user}}…",
    filter_agent: "Filter by agent",
    filter_errors_only: "Errors only",
    filter_pending_only: "Awaiting approval",
    auto_refresh: "Auto-refresh (30s)",
    filter_window: "Time window",
    window_all: "All time",
    window_1h: "Last hour",
    window_24h: "Last 24 hours",
    window_7d: "Last 7 days",
    pager_total: "{{total}} conversations",
    search_placeholder: "Search title or message content…",
    cross_tenant_banner: "cross-tenant view",
    failed_to_load: "Failed to load conversations",
    error_count: "{{count}} failed run(s)",
    pending_count: "{{count}} awaiting approval",
    empty_home: "No conversations yet.",
    empty_cross: "No conversations across tenants.",
  },
  conversations_detail: {
    failed_to_load: "Failed to load conversation",
    thread_label: "Thread",
    summary_title: "Conversation summary",
    user: "User",
    tokens: "Tokens",
    tokens_value: "{{total}} ({{input}} in / {{output}} out, {{calls}} calls)",
    models: "Models",
    messages_title: "Messages",
    messages_empty: "No messages recorded for this conversation.",
    role_user: "User",
    role_assistant: "Agent",
    runs_title: "Runs",
    runs_empty: "No runs in this conversation.",
    column_started: "Started",
    view_run: "View run",
  },
  skills_tab: {
    title: "Skills authored by this agent",
    authored_hint: "Skills created by {{agent}} (all versions)",
    col_name: "Name",
    col_status: "Status",
    col_visibility: "Visibility",
    col_version: "Latest",
    col_created: "Created",
    empty: "This agent has not authored any skills yet.",
  },
  triggers_tab: {
    title: "Triggers bound to this version",
    manage_hint: "Create / enable / disable on the Triggers page",
    col_name: "Name",
    col_kind: "Kind",
    col_enabled: "State",
    col_source: "Source",
    col_created: "Created",
    enabled: "enabled",
    disabled: "disabled",
    empty: "No triggers bound to this agent version.",
  },
  memory_tab: {
    col_kind: "Kind",
    col_content: "Content",
    col_created: "Created",
    col_source: "Source",
    source_run: "View run",
    as_of_placeholder: "View memories as of…",
    as_of_banner: "Historical view — memories valid at {{at}}",
  },
  knowledge_page: {
    page_title: "Knowledge",
    subtitle: "Tenant-shared knowledge bases feeding agent retrieval.",
    failed_to_load: "Failed to load knowledge bases",
    bases_title: "Bases",
    bases_empty: "No knowledge bases yet.",
    empty_cross: "No tenant has any knowledge bases yet.",
    col_tenant: "Tenant",
    create_base: "New base",
    create_duplicate: "A knowledge base with this name already exists.",
    field_name: "Name",
    field_chunk_max: "Chunk max tokens (optional)",
    field_chunk_overlap: "Chunk overlap tokens (optional)",
    field_chunk_hint:
      "Defaults apply when left empty; overlap must be less than max.",
    col_base_name: "Name",
    col_chunking: "Chunk max/overlap",
    delete: "Delete",
    delete_base_confirm_title: "Delete base {{name}}?",
    delete_base_confirm_body:
      "This removes the base together with all its documents and vectors.",
    documents_title: "Documents in {{name}}",
    documents_unselected: "Documents",
    documents_empty: "No documents in this base yet.",
    select_hint: "Select a base on the left to manage its documents.",
    upload: "Upload",
    unsupported_type: "Unsupported document type: {{name}}",
    embedder_missing:
      "Document ingestion is unavailable — no embedding model is configured on the platform.",
    col_filename: "Filename",
    col_status: "Status",
    col_chunks: "Chunks",
    col_updated: "Updated",
    delete_doc_confirm_title: "Delete document {{name}}?",
    field_description: "Description",
    field_description_placeholder:
      "What this knowledge base is for (optional).",
    advanced: "Advanced",
    retrieval_defaults: "Retrieval defaults",
    field_top_k: "Results (top-k)",
    field_top_k_hint: "How many chunks knowledge_search returns by default.",
    field_threshold: "Score threshold",
    field_threshold_hint:
      "Drop vector hits below this similarity (0–1). Off by default (empty). To enable, tune it from the retrieval test (try ~0.2 first) — too high drops relevant hits.",
    field_method: "Retrieval method",
    field_method_hint:
      "Hybrid fuses vector + keyword recall; restrict to one if needed.",
    method_vector: "Vector",
    method_keyword: "Keyword",
    method_hybrid: "Hybrid",
    field_rerank: "LLM rerank",
    field_rerank_hint:
      "Reorder fused results by an LLM when one is configured.",
    col_description: "Description",
    col_documents: "Docs",
    col_chunks_total: "Chunks",
    needs_reindex_tag: "Needs re-index",
    reindexing_tag: "Re-indexing",
    status_pending: "Pending",
    status_processing: "Processing",
    status_ready: "Ready",
    status_failed: "Failed",
    failed_to_load_base: "Failed to load knowledge base",
    tab_documents: "Documents",
    tab_test: "Retrieval test",
    tab_settings: "Settings",
    stat_documents: "Documents",
    stat_chunks: "Chunks",
    stat_embedding: "Embedding model",
    embedding_unset: "Not set",
    needs_reindex_banner:
      "The platform embedding model changed since this base was indexed. Re-index to keep retrieval accurate.",
    reindex_button: "Re-index",
    reindex_started: "Re-indexing started; it runs in the background.",
    upload_dragger_hint: "Click or drag files here to upload",
    upload_dragger_sub:
      "PDF, Office, Markdown, HTML, CSV, TXT — multiple files supported.",
    col_attempts: "Attempts",
    reingest: "Re-ingest",
    reingest_started: "Re-ingestion started.",
    reingest_no_bytes:
      "The original file was not retained for this document — please re-upload it.",
    view_chunks: "View chunks",
    chunks_drawer_title: "Chunks · {{name}}",
    chunks_empty: "No chunks (the document may still be processing).",
    chunk_label: "Chunk #{{index}}",
    test_title: "Retrieval test",
    test_query_placeholder:
      "Enter a query to see which chunks would be retrieved…",
    test_run: "Run",
    test_empty_hint:
      "Run a query to preview retrieval — ranked chunks with scores and recall path.",
    test_no_results: "No chunks matched this query.",
    test_control_top_k: "Top-k",
    test_control_method: "Method",
    test_control_threshold: "Threshold",
    test_control_rerank: "Rerank",
    test_method_default: "Base default",
    recall_vector: "vector",
    recall_keyword: "keyword",
    recall_both: "both",
    settings_title: "Retrieval settings",
    settings_saved: "Settings saved.",
    settings_rename_note:
      "Renaming is not supported — agents reference a base by name.",
    settings_embedding_title: "Embedding",
  },
  rate_card_page: {
    page_title: "Model Pricing",
    subtitle:
      "Platform model pricing — CNY per million tokens (decimals allowed).",
    system_admin_only: "Model pricing is platform-admin (system_admin) only.",
    failed_to_load: "Failed to load pricing",
    empty: "No pricing rows yet.",
    filter_provider: "Provider",
    filter_model: "Model",
    create: "New price",
    edit: "Edit",
    edit_title: "Edit {{provider}} / {{model}}",
    delete: "Delete",
    delete_confirm_title: "Delete this pricing row?",
    delete_confirm_body:
      "Historical cost already computed is unaffected; future rollups use the current price.",
    col_provider: "Provider",
    col_model: "Model",
    col_input: "Input (¥/M)",
    col_output: "Output (¥/M)",
    col_cache_creation: "Cache write (¥/M)",
    col_cache_read: "Cache read (¥/M)",
    field_provider: "Provider",
    field_model: "Model",
    field_input: "Input price (CNY / million tokens)",
    field_output: "Output price (CNY / million tokens)",
    field_cache_creation: "Cache-write price (CNY / million tokens)",
    field_cache_read: "Cache-read price (CNY / million tokens)",
    identity_immutable:
      "Provider / model are this row's identity and cannot change — edit the prices to reprice.",
  },
  artifacts_page: {
    col_name: "Name",
    col_kind: "Kind",
    col_latest: "Latest",
    download: "Download",
    download_failed: "Download failed: {{detail}}",
    versions: "Versions",
    versions_title: "Versions of {{name}}",
    versions_empty: "No versions recorded.",
    ver_col_version: "Version",
    ver_col_path: "Workspace path",
    ver_col_size: "Size (bytes)",
    ver_col_created: "Created",
    digest_pending: "Backfilled after the first download",
    delete: "Delete",
    deleted: "Soft-deleted {{name}}",
    delete_confirm_title: "Delete {{name}}?",
    delete_confirm_body:
      "Soft-delete: the artifact disappears from lists and downloads; bytes stay until the retention sweep, and re-saving the same name un-deletes it.",
  },
  manifest_tab: {
    hint: "Visual config — edit the fields and Save. The YAML tab is a raw escape hatch.",
    save: "Save",
    reset: "Reset",
    save_failed: "Failed to save manifest",
  },
  manifest_editor: {
    tab_basic: "Basics",
    tab_model: "Model",
    tab_prompt: "Prompt",
    tab_tools: "Tools",
    tab_mcp: "MCP",
    tab_knowledge: "Knowledge",
    tab_skills: "Skills",
    tab_subagents: "Sub-agents",
    tab_memory: "Memory",
    tab_governance: "Governance",
    tab_defenses: "Defenses",
    tab_yaml: "YAML",
    loading_schema: "Loading schema…",
    schema_load_failed: "Failed to load the manifest schema",
    invalid_yaml_title: "Can't switch to Form",
    invalid_yaml_hint:
      "The YAML is invalid or doesn't match the manifest schema. Fix it here first.",
    search_placeholder: "Search settings…",
    group_basic: "Basic",
    group_model: "Model & Routing",
    group_prompt: "Prompt & Output",
    group_capabilities: "Capabilities",
    group_memory: "Memory",
    group_budget: "Run Budget & Timeouts",
    group_context: "Context & Compression",
    group_security: "Security & Defenses",
    group_sandbox: "Sandbox & Resources",
    group_observability: "Triggers & Observability",
    field_impact_label: "Impact",
    field_customized_badge: "Customized",
    field_reset: "Reset to default",
    field_reset_hint: "Reset to default: {{value}}",
  },
  run_profile: {
    title: "Run profile",
    hint: "Pick a profile to auto-configure memory, steps, and compression — every group below can still be fine-tuned field by field.",
    balanced: "Balanced (recommended)",
    balanced_desc: "Solid for everyday use at moderate cost (default).",
    cost: "Cost-saving",
    cost_desc:
      "Saves tokens: remembers and thinks less, compresses sooner — cheaper on long conversations.",
    capability: "High-capability",
    capability_desc:
      "Remembers and thinks more: double the steps, fuller memory, steadier on hard tasks — costs more.",
    custom: "Custom",
    nearest_hint: 'Closest to "{{name}}" — {{count}} setting(s) apart',
    confirm_title: 'Apply "{{name}}"?',
    confirm_body:
      "This will change {{count}} settings; you can still adjust any of them afterward.",
  },
  run_budget: {
    subhead_steps: "Steps & flow",
    max_iterations_label: "Max steps",
    max_iterations_brief: "How many steps this run can take at most",
    max_iterations_impact:
      "A 'step' is one think-then-act cycle — the model thinks, then does one thing (like a tool call). This caps how many of those cycles a single run may go through. Once the budget runs out, the agent is forced to wrap up right away — no more tool calls, just a summary — so the answer may be incomplete.\nRaise it for research-heavy agents that need many lookups (40-60 is a common range for heavy tasks); lower it to keep costs down.\nIf this run spins up helper sub-tasks along the way, each helper's own step budget is capped at whichever is smaller: this value, or the platform's worker ceiling.\nExample: an agent researching an industry across a dozen websites will get cut off early and give an incomplete answer if this is set too low.",
    max_iterations_default: "30",
    wf_type_label: "Workflow type",
    wf_type_brief: "Think-and-act as it goes, or plan first then execute",
    wf_type_impact:
      "react (recommended): the agent thinks as it goes — simple requests get answered directly, and only complex ones get a step-by-step plan first. This covers most cases. plan_execute: every task gets a full plan up front no matter how simple, then runs it step by step.\nplan_execute is more reliable for long, sequential jobs like writing a report, but even trivial requests pay the planning cost — slower and pricier, since it's one extra model call.\nThe model that does the planning is set by a rule in the Model & Routing group; if none is set, the main model handles it. custom is deprecated — picking it behaves exactly like react.",
    wf_type_default: "react",
    wf_type_opt_react: "react (think, then act)",
    wf_type_opt_plan_execute: "plan_execute (plan, then execute)",
    wf_type_opt_custom: "custom (deprecated — same as react)",
    max_no_progress_label: "No-progress stop",
    max_no_progress_brief: "Stop early once it's stuck making no progress",
    max_no_progress_impact:
      "Stop early after N consecutive steps with no real progress at all, so the agent doesn't burn through steps for nothing; 0 = turns this check off.\nRaise it to tolerate legitimate retries without cutting them off; lower it to kill a stuck loop faster — but too low may cut off a normal retry, so 3-5 is a reasonable starting point.\nExample: the agent keeps calling the same tool over and over without getting anywhere useful — this setting stops it early instead of letting it spin in place.",
    max_no_progress_default: "0 (off)",
    subhead_time: "Time & spend",
    run_deadline_label: "Max run time",
    run_deadline_brief: "The longest this run may take",
    run_deadline_impact:
      "The longest this run may take, in seconds, including any sub-agents it spawns along the way; once exceeded, the run is force-stopped, so a stuck run can't quietly run up the bill.\n0 doesn't mean unlimited — it just falls back to the platform's own default cap (currently 3600 seconds / 1 hour, set by ops). Set an explicit number here for a longer run or tighter control.\nExample: a task doing live web lookups gets stuck in a loop — this cap force-kills it instead of letting it burn on forever.",
    run_deadline_default: "0 (platform default, 1h)",
    stream_deadline_label: "First-token timeout",
    stream_deadline_brief: "The longest wait for the model to start replying",
    stream_deadline_impact:
      "How long this may wait, from calling the model, until its first word comes back. Defaults to 180 seconds, enough for slow jobs like long report generation. This is different from the inter-token idle timeout below, which only starts counting after the first word arrives.\n0 = no limit (only recommended for development or long batch jobs).\nExample: the model provider occasionally hangs and never starts replying — once this timeout passes, the system automatically switches to a backup model instead of leaving the whole run hanging.",
    stream_deadline_default: "180",
    idle_timeout_label: "Inter-token idle timeout",
    idle_timeout_brief: "The longest pause allowed mid-answer",
    idle_timeout_impact:
      "Once the model starts answering, if the gap between two words grows longer than this, it's treated as the model going silent mid-answer (stuck, or it just stopped). Different from the first-token timeout above: that one covers the wait before the answer starts, this one covers pauses after it's already started.\n0 = no limit (only recommended for development or long batch jobs); providers that don't stream ignore this.\nExample: a support agent's answer is half-typed when the model hangs — once this gap passes with no new words, the run ends that turn early and keeps whatever was generated so far, instead of leaving the user staring at a frozen answer.",
    idle_timeout_default: "45",
    token_budget_label: "Token budget",
    token_budget_brief: "The total token spend cap for this run",
    token_budget_impact:
      "Sets a spending cap for this run, counted in tokens (covers every sub-task it kicks off too). At 80% of the cap, the model gets nudged to wrap up soon; once it's used up, the model is forced to give its best answer from what it already has, without calling any more tools.\n0 = no cap.\nExample: set the cap to 200,000 tokens — at 160,000 the model gets nudged to wrap up, and at 200,000 it must answer from what it already has.",
    token_budget_default: "0 (disabled)",
  },
  context_gates: {
    group_intro:
      "When content overflows the window, it falls back on the three steps below in order — the first two are usually enough. Window size follows the selected model.",
    tab_intro_prune:
      "Step 1: older tool results get squashed into a one-line reference first — the cheapest and least lossy of the three steps.",
    tab_intro_window:
      "Step 2: if pruning isn't enough, only the first turn plus the most recent turns are sent — the real history is never touched.",
    tab_intro_compress:
      "Step 3: when the first two steps still aren't enough, the model summarizes the long middle stretch into one background note — the last resort.",
    panel_tool_result_prune: "Step 1 · Tool-result prune",
    panel_working_memory: "Step 2 · Sliding window",
    panel_context_compression: "Step 3 · Context compression",
    pr_enabled_label: "Enable tool-result prune",
    pr_enabled_brief: "Over the limit, squash old tool results to one line",
    pr_enabled_impact:
      "The cheapest and least lossy of the three steps: the full conversation and all reasoning stay intact — only older tool results get swapped for a one-line reference (and only ones already saved to the workspace, so they're always recoverable). This only affects what's sent this one time; the real history is never rewritten.\nTurn it off and mild overflows fall to the next two steps (trimming, then summarizing) sooner.\nExample: a tool call returns a few thousand lines of logs — after pruning, the model just sees 'ran xxx, see the workspace for details', keeping the prompt clean while the raw log stays retrievable if needed.",
    pr_enabled_default: "true",
    pr_threshold_pct_label: "Trigger threshold (% of window)",
    pr_threshold_pct_brief: "Only prunes once content hits this % of window",
    pr_threshold_pct_impact:
      "Uses the same ratio setup as the sliding window and context compression below, but each is configured independently.\nLower = prunes earlier, cheaper; higher = keeps more original content but leans more on the two later steps. Range 0.05–1.\nExample: a 100k-token window with this set to 0.7 means pruning only kicks in once the estimated content passes 70k tokens.",
    pr_threshold_pct_default: "0.7",
    pr_recent_kept_label: "Recent tool results kept",
    pr_recent_kept_brief: "The most recent few results stay intact",
    pr_recent_kept_impact:
      "Over the limit, this many of the most recent tool results stay intact; older ones get squashed.\nHigher = the model sees more intact results, at the cost of a bigger prompt; 0 = once over the threshold, every tool result gets squashed, none kept in full.\nExample: set to 4 — when over the limit, the 4 most recent tool results stay intact and everything older gets squashed to a one-line reference.",
    pr_recent_kept_default: "4",
    wm_enabled_label: "Enable sliding window",
    wm_enabled_brief: "Over the limit, keep only the first turn plus recent ones",
    wm_enabled_impact:
      "Only ever cuts at a user-message boundary — never splits a tool call from its result. This only affects what's sent this one turn; the next turn re-trims from the full history again.\nOn = most mild overflows are solved right here, saving the cost of the summarization step below; off = overflows fall through to context compression sooner instead.\nExample: a 30-turn conversation goes over the limit — the sliding window automatically sends the model only turn 1 plus the most recent handful, leaving the middle out for now (the full history is still intact).",
    wm_enabled_default: "true",
    wm_threshold_pct_label: "Trigger threshold (% of window)",
    wm_threshold_pct_brief: "Only trims once content hits this % of window",
    wm_threshold_pct_impact:
      "Uses the same ratio setup as tool-result prune above and context compression below, but each is configured independently.\nLower = trims earlier, cheaper; higher = keeps more of the conversation but leans more on context compression to catch the rest. Range 0.05–1.\nExample: a 100k-token window with this set to 0.7 means trimming only kicks in once content passes 70k tokens.",
    wm_threshold_pct_default: "0.7",
    wm_max_recent_turns_label: "Recent turns kept",
    wm_max_recent_turns_brief:
      "How many of the most recent turns the window keeps",
    wm_max_recent_turns_impact:
      "Lower = cheaper, but drops more of the middle of the conversation (the dropped part isn't summarized anywhere — that's context compression's job); for long, many-turn conversations, 10 or more is a reasonable floor.\nExample: a 20-turn Q&A conversation with this set to 10 — once over the limit, only the most recent 10 turns get sent and the earlier 10 get trimmed.",
    wm_max_recent_turns_default: "20",
    wm_keep_first_turn_label: "Keep first turn",
    wm_keep_first_turn_brief: "Always keeps the very first turn when trimming",
    wm_keep_first_turn_impact:
      "Anchors the originally-stated task so later trimming can't sweep it away.\nTurn it off and a long conversation can lose the original task description — generally not recommended.\nExample: the opening turn says 'answer only in Chinese' — if the turn holding that instruction got trimmed away, the model might forget it downstream; keeping the first turn prevents exactly that.",
    wm_keep_first_turn_default: "true",
    cc_enabled_label: "Enable context compression",
    cc_enabled_brief: "Over the limit, have a model summarize the middle",
    cc_enabled_impact:
      "The last of the three steps: keeps the first N and last M messages untouched, and replaces everything in between with a single 〈context-summary〉 note; before anything is dropped, if long-term memory is on, the key points get saved there first.\nTurn it off and, if the first two steps aren't enough, an oversized conversation just fails outright — there's nothing left to fall back on.\nExample: a long, multi-turn troubleshooting thread — the opening messages and the most recent ones stay as-is, while dozens of turns in the middle get folded into one summary paragraph; the model still knows the gist, just not the verbatim detail.",
    cc_enabled_default: "true",
    cc_threshold_pct_label: "Trigger threshold (% of window)",
    cc_threshold_pct_brief: "Only compresses once content hits this % of window",
    cc_threshold_pct_impact:
      "Uses the same ratio setup as tool-result prune and the sliding window above, but each is configured independently.\nLower = compresses earlier, cheaper; higher = keeps more of the original text but is riskier, since this is the last step — nothing catches an overflow after this. Range 0.05–1.\nExample: a 100k-token window with this set to 0.7 means compression only kicks in once content passes 70k tokens.",
    cc_threshold_pct_default: "0.7",
    cc_head_keep_label: "Head entries kept",
    cc_head_keep_brief: "Keeps the first N messages untouched",
    cc_head_keep_impact:
      "Includes wherever memory content gets injected; if this is set to 0 while memory injection is on, the runtime automatically bumps it to 1 (it never actually runs at 0).\nExample: set to 4 — when summarizing, the first 4 messages (typically the system setup and the original task) stay untouched instead of getting folded into the summary.",
    cc_head_keep_default: "4",
    cc_tail_keep_label: "Tail entries kept",
    cc_tail_keep_brief: "Keeps the most recent M messages untouched",
    cc_tail_keep_impact:
      "The tail is whatever's currently being worked on — set it too low and recent details get lost.\nExample: set to 6 — when summarizing, the 6 most recent messages stay untouched, so the model doesn't lose track of what just happened.",
    cc_tail_keep_default: "6",
    cc_flush_before_compaction_label: "Flush to memory before compacting",
    cc_flush_before_compaction_brief: "Saves key points before dropping them",
    cc_flush_before_compaction_impact:
      "Keeps important decisions from being lost after repeated compressions.\nOnly actually does anything when 'Learn (remember new info)' is on — otherwise this switch is a no-op.\nExample: the user confirmed an important decision somewhere in the middle of the conversation — before that stretch gets summarized away, the key point gets saved to long-term memory first, so it's still findable later.",
    cc_flush_before_compaction_default: "true",
    cc_max_passes_label: "Max compression passes",
    cc_max_passes_brief: "Still over the limit? Retry at most N more times",
    cc_max_passes_impact:
      "A one-off failure in the summarization model just skips that pass and retries; only 3 failures in a row actually fails the run.\nStill over the threshold after using up these retries? The run just fails outright (a context-overflow error, not a silent workaround).\nExample: set to 3 — if it's still over the threshold after one compression pass, it tries again, up to 3 total; if 3 passes still aren't enough, the run fails.",
    cc_max_passes_default: "3",
    cc_pressure_feedback_label: "Pressure-feedback nudge",
    cc_pressure_feedback_brief: "Nudges the model to trim down near the limit",
    cc_pressure_feedback_impact:
      "Once usage hits window × warn %, a reminder gets tacked onto the last message so the model knows to wrap up (it never touches the system prompt, so prompt caching stays intact).\nBelow that percentage, nothing is added.\nExample: usage just crosses the warn line — the model starts trimming its own explanations and wrapping up sooner, instead of waiting to get hard-trimmed once it's actually over the limit.",
    cc_pressure_feedback_default: "true",
    cc_pressure_warn_pct_label: "Pressure warn %",
    cc_pressure_warn_pct_brief:
      "The window percentage that triggers the budget reminder",
    cc_pressure_warn_pct_impact:
      "Should, in theory, be lower than the compression threshold for the reminder to actually give a head start.\nBut by default the warning is 0.75 and compression is 0.7, meaning compression currently fires BEFORE the warning does — lower this value if you want the reminder to actually come first.",
    cc_pressure_warn_pct_default: "0.75",
    budget_enabled_label: "Enable tool-output budget",
    budget_enabled_brief: "Master switch for handling oversized tool output",
    budget_enabled_impact:
      "Only takes effect when both switches are on: the platform-wide one and this one (if the platform switch is off, turning this on does nothing). This doesn't affect the separate, older overflow-handling built into bash/exec/http/mcp, which keeps working regardless.\nOn = oversized tool results get moved to the workspace, and the model just sees a reference; off = oversized tool results no longer get saved to the workspace at all, and anything too long just gets truncated, losing whatever falls past the cut.\nExample: a web scrape returns a few hundred thousand characters of raw HTML — with this on, it gets moved to the workspace and the model just sees a reference to it.",
    budget_enabled_default: "true",
  },
  security_gates: {
    tab_defenses: "Defenses",
    tab_approval: "Human approval",
    tab_network: "Subtasks & network",
    panel_network: "① Network egress",
    panel_enforce: "② Tool-use enforcement",
    egress_label: "Egress mode",
    egress_brief: "The sandbox's master switch for reaching the outside world",
    egress_impact:
      "proxy (default, recommended) = everything goes through the credentialed proxy, fully audited; direct = traffic goes straight out with no proxy, so there's no automatic credential injection and no centralized audit trail — generally not recommended; none = the sandbox can't reach the outside world at all, every request fails.\nExample: an agent that only needs to read and write your own database can be set to none to rule out any outbound access entirely.",
    egress_default: "proxy",
    egress_opt_proxy: "Proxy (default)",
    egress_opt_direct: "Direct",
    egress_opt_none: "None (blocked)",
    allowlist_label: "Domain allowlist",
    allowlist_brief: "Restricts which domains can be reached",
    allowlist_impact:
      "Non-empty = only these domains are reachable; empty = the whole public internet is open (spoofed requests and internal-network probing are still blocked, and everything is audited). Matches an exact domain or its subdomains, and wildcards like ['*'] aren't allowed — submitting one gets rejected outright.\nIf both an allowlist and a denylist are set, the denylist wins.\nExample: list only api.weather.com and this agent can reach nothing but that weather API — every other address gets blocked.",
    allowlist_default: "Empty (open to the public internet)",
    denylist_label: "Domain denylist",
    denylist_brief: "Always blocks the domains listed here",
    denylist_impact:
      "Regardless of the allowlist or the default-open behavior, these domains are always blocked. Takes priority over the allowlist, and matches the same way — exact domain or subdomain.\nExample: keep the public internet open overall but explicitly ban a competitor's domain or a known phishing domain — good for 'leave everything open, just block these few.'",
    denylist_default: "Empty",
    enforce_label: "Tool-use enforcement",
    enforce_brief: "Forces the model to actually check, not make things up",
    enforce_impact:
      "Adds a hard requirement to the system prompt: for anything needing real-time facts, actually call a tool, act right away, and never make up a tool result.\nauto (default) = turned on for every model except the families — Claude, GPT, etc. — that are already reliable about calling tools on their own; on/off = force it on or off regardless of which model is in use.\nExample: a newly onboarded weak model gets this safety net for free under auto, no config change needed.",
    enforce_default: "auto",
    enforce_opt_auto: "Auto (default)",
    enforce_opt_on: "Always on",
    enforce_opt_off: "Always off",
    dict_note:
      "Rate limiting, PII redaction, and security policy are advanced items — configure them in the YAML view.",
  },
  sandbox_group: {
    pw_label: "Project plan to workspace",
    pw_brief: "Syncs plan/progress files into the workspace",
    pw_impact:
      "At the end of each turn, write PLAN.md / TODO.md / MEMORY.md into the user's workspace, and read them back when the next run starts — so progress is visible both inside and outside the sandbox. It also needs the deployment to have the sandbox supervisor wired up to actually take effect.\nThis switch only controls whether that file sync happens — it has nothing to do with whether files stick around. A run tied to a user identity already keeps its workspace files regardless (restored automatically next time, even after an idle cleanup); a system run with no user identity always gets a throwaway workspace — neither case depends on this switch.\nExample: a long research task gets interrupted midway — with this on, the next run can pick up right where PLAN.md left off; with this off, the agent still knows the plan, it just doesn't write these files to sync it.",
    platform_note_title: "Platform-effective values",
    platform_note_intro:
      "The following are decided by the platform deployment — the manifest has no say:",
    pn_image: "Image",
    pn_image_v: "Platform-shared image (Python plus common office/data/media libraries)",
    pn_resources: "Resources",
    pn_resources_v: "Default 1 CPU / 1024 MB memory / up to 128 processes",
    pn_timeout: "Command timeout",
    pn_timeout_v: "30s default, per-tool-call override capped at 300s",
    pn_paths: "Writable paths",
    pn_paths_v: "Only /workspace and /tmp (root filesystem is read-only)",
    pn_runtime: "Container runtime",
    pn_runtime_v: "gVisor or runc, set by a deployment environment variable",
    declarative_note:
      "The manifest's runtime / image / image_build / resources / readonly_root / writable / mounts fields and the code block are declarative-only: writing them doesn't error, but nothing reads them at run time. To change real limits, edit the platform deployment (sandbox-supervisor environment variables).",
  },
  memory_group: {
    tab_basic: "Basic",
    tab_retrieval: "Retrieval details",
    tab_budget: "Budget & housekeeping",
    on_label: "Long-term memory",
    on_brief: "Remembers you across chats",
    on_impact:
      "When on, the agent remembers user info and past interactions across sessions and recalls them automatically;\noff = every chat starts from scratch.\nNeeds a platform embedding model configured, or turning this on won't actually do anything.\nExample: on, recall 5",
    topk_label: "Memories recalled per run",
    topk_brief: "How many memories it recalls each turn",
    topk_impact:
      "How many of the most relevant memories to pull in per chat.\nToo many crowd the context; too few miss info.\nExample: 5",
    write_back_label: "Learn (remember new info)",
    write_back_brief: "Automatically remembers new info from the chat",
    write_back_impact:
      "When on, each conversation ends by saving what it learned, recalled automatically next time.\nOff = only use existing memories, never add.\nExample: on",
    verify_reads_label: "Check memories before answering",
    verify_reads_brief: "Double-checks memories first — slower but more accurate",
    verify_reads_impact:
      "Before answering, the model screens the recalled memories and drops anything irrelevant, stale, or contradictory, so old memories don't skew the answer.\nThe cost is one extra model call: measured at roughly 0.7–1s of added wait per answer.\nIf the screening itself fails, nothing is blocked — all memories are used as-is.\nOn by default.\nExample: on",
    write_min_importance_label: "Importance filter",
    write_min_importance_brief: "Skips memories below this importance score",
    write_min_importance_impact:
      "Each thing worth remembering gets an importance score (0–1); anything below this isn't saved, so small talk doesn't get stored.\n0.3: drop the clearly trivial; 0: keep everything.\nExample: 0.3",
    reconcile_writes_label: "Tidy up (dedup/merge)",
    reconcile_writes_brief: "Auto-merges duplicate memories before saving",
    reconcile_writes_impact:
      "Before saving new info, compare it with similar existing memories and merge, update, or delete as needed, avoiding pile-ups and contradictions.\nOff = just append.\nOn by default.\nExample: on",
    recall_mode_label: "Where memories are inserted",
    recall_mode_brief: "Controls how often memories get re-inserted",
    recall_mode_impact:
      "Where memories go in the conversation.\nPer session: inserted once for the whole session — cheaper and faster (default).\nPer turn: re-inserted every turn — for agents that edit their own memory mid-conversation.\nExample: per session",
    recall_mode_per_session: "Per session (cheaper, faster)",
    recall_mode_per_turn: "Per turn",
    rewrite_reads_label: "Rewrite question for recall",
    rewrite_reads_brief: "Rewrites your question before searching memories",
    rewrite_reads_impact:
      "Before searching memories, rewrite the user's latest message into a short standalone search query — stripping instructions and trimming long messages so they don't skew what's recalled.\nCosts one extra model call; if it errors, the original message is used.\nOff by default (opt-in).\nExample: off",
    abstain_threshold_label: "Skip weak matches (threshold)",
    abstain_threshold_brief: "Skips memories that don't match well enough",
    abstain_threshold_impact:
      "After searching, if the best memory's similarity to the question is below this, inject nothing instead of a weak match — avoids dragging loosely-related memories into the answer.\n0 = never skip (default); ~0.2–0.3 = skip clearly weak matches.\nExample: 0",
    panel_injection: "① Injection budget",
    panel_consolidation: "② Background memory consolidation",
    inj_budget_label: "Memory injection token budget",
    inj_budget_brief: "The most tokens memories may take up in the prompt",
    inj_budget_impact:
      "The most tokens recalled memories may take up in the prompt — packed in from most to least relevant until this fills up.\nRaise it for more memory context at a higher per-turn cost; lower it to save tokens, at the risk of cutting off long memories.\nHow many memories get recalled is capped by 'memories recalled per run', but a single unusually long memory can still blow up the space it takes — this budget is the backstop (whichever entry lands on the boundary gets truncated and flagged).",
    inj_budget_default: "2000",
    corr_budget_label: "User-correction reserve budget",
    corr_budget_brief: "Reserved budget for corrections the user made",
    corr_budget_impact:
      "A dedicated token allowance that explicit user corrections (the kind the system treats as 100% certain) get first claim on, so ordinary memories can't crowd them out: correction memories get first claim on up to this many tokens, and ordinary memories only get what's left.\nSet it to 0 to skip this protection.",
    corr_budget_default: "500",
    consolidation_label: "Background memory consolidation",
    consolidation_brief: "Tidies memory in the background, without slowing chats",
    consolidation_impact:
      "Runs in the background on a schedule (every 4 hours by default), merging similar short-term memories and clearing out noisy ones — it's a background job, so it never slows down the conversation.\nTurn it off and this agent's long-term memory stops auto-deduplicating, distilling, and cleaning itself up — short-term memories just keep piling up.\nThe cleanup runs on the auxiliary model and is billed separately under 'memory consolidation'; what counts as 'similar enough to merge' is a tenant-level setting, not something this manifest controls.",
    aux_model_note:
      "The model that does the cleanup defaults to the platform-wide setting (claude-sonnet-4-6).\nTo pick a different one for just this agent, edit policies.memory_consolidation.aux_model in the YAML view.",
  },
  model_group: {
    panel_reflection: "Reflection self-assessment",
    rf_enable_label: "Reflection self-assessment",
    rf_enable_brief: "Has a judge model score the answer before it's sent",
    rf_enable_impact:
      "Before an answer is final, a judge model scores it and flags problems, and automatically sends it back for another pass if it falls short — repeating until it passes or the retry cap runs out. Which model judges is set by the 'Reflection evaluator model' picker that appears below once this is on; if it's unset, the main model judges its own answer.\nEach extra round of reflection is one more model call: better quality, but slower and pricier.\nExample: a support agent's answer is missing the refund process — the judge catches it and sends it back for a redo before the user ever sees it.",
    rf_budget_label: "Reflection retry cap",
    rf_budget_brief: "The most reflection rounds before it just ships",
    rf_budget_impact:
      "The most reflection rounds a single run may use — once it hits this cap, it accepts the current answer regardless of whether it passed, instead of looping forever.\nRaise it for more rounds of self-improvement — cost and time scale up roughly in step; 1 = judged once, no retries.\nExample: set to 2 — the judge flags the first answer, one improved pass is made, and whatever comes out of that second pass ships, pass or not.",
    rf_budget_default: "2",
    rf_deadline_label: "Per-reflection time limit (seconds)",
    rf_deadline_brief: "The most seconds a single reflection round may take",
    rf_deadline_impact:
      "The most seconds a single reflection round may take before the current answer is accepted as-is instead of waiting on the judge any longer — keeps a stuck judge model from dragging down the whole run.\nRaise it to tolerate a slow model; lower it to keep things responsive. Capped at 600 seconds.\nExample: the judge model slows down and is still scoring past the timeout — that round is simply abandoned and the existing answer moves forward.",
    rf_deadline_default: "30",
    yaml_note:
      "Advanced items like the planning-model routing rule (routing.rules, used when the workflow type is plan_execute), the vision-model fallback chain, and connection settings (base_url, Azure deployment, etc.) still need the YAML view.\napi_key_ref is deprecated: setting it in the manifest gets ignored, with a warning.",
  },
  observability_group: {
    resp_cache_label: "LLM response cache",
    resp_cache_brief: "Reuses the answer to a question it's seen before",
    resp_cache_impact:
      "A repeated request reuses last time's full answer instead of calling the model again — cheaper and faster. A cache hit skips the model call entirely — no provider selection, no fallback chain, none of it.\nTurn this off for an agent whose prompt has time-sensitive content (today's date, live data, etc.), or it may hand back a stale answer.\nThis is a different thing from the Model & Routing group's 'prompt caching' (Anthropic prompt caching): that one only saves on input tokens, while this switch reuses the entire answer as-is.",
    traj_rec_label: "Run archiving",
    traj_rec_brief: "Archives full runs for quality review",
    traj_rec_impact:
      "When on, every run's full transcript — the user's messages, the agent's replies, and its tool calls — gets archived once the run finishes; the platform uses that archive for quality evaluation, and it may also feed future model improvements.\nOff = conversation content never gets archived, which fits agents that handle sensitive information.\nExample: a support agent that touches private customer data turns this off, so run content stays only in the session log and never enters the analysis archive.",
    triggers_note:
      "Create and manage scheduled tasks from the Triggers page; the manifest's triggers field is not wired up and has no effect.",
    declarative_note:
      "observability's trace / log_level / redact_fields are declarative-only — nothing reads them at run time. Tracing, log level and PII redaction are all platform-configured.",
  },
  model_select: {
    provider_label: "Provider",
    provider_placeholder: "Select a configured provider",
    model_label: "Model",
    model_placeholder: "Select a model",
    vision_on: "Vision: supported",
    vision_off: "Vision: not supported",
    temperature: "Temperature",
    advanced: "Advanced",
    context_window: "Context window",
    context_window_hint:
      "Tokens; sets the compaction threshold. Empty = the model catalog default.",
    thinking_label: "Thinking mode",
    thinking_cannot_disable:
      "This model can only minimise thinking, not fully disable it.",
    effort_label: "Effort",
    effort_hint:
      "Reasoning depth tier. Empty = the provider default. Only settable when the model catalog marks the model as thinking-capable — otherwise the build fails.",
    adaptive_label: "Adaptive thinking",
    adaptive_hint:
      "Lets the model decide its own thinking depth based on task difficulty (Anthropic 4.6+). Anthropic-only.",
    cache_label: "Prompt caching",
    cache_hint:
      "Anthropic prompt caching — significant savings on long sessions. On by default; turn off for agents whose prompt holds time-sensitive content. Anthropic-only.",
    max_tokens_hint:
      "Output token ceiling for a single reply. Only effective on the Anthropic path (qwen/doubao borrow it to derive the thinking budget); OpenAI-family providers ignore this value.",
    rate_limit_hint:
      "Request-rate ceiling for this model (per minute). Requests over the limit queue instead of erroring.",
  },
  agent_form: {
    section_basic: "Basics",
    field_name: "Name",
    field_name_required: "Name is required",
    field_name_placeholder: "my-agent",
    field_description: "Description",
    basic_yaml_note:
      "Advanced items like template inheritance (extends) and tenant compliance tags (compliance_pack, etc.) need the YAML view.\nThe latter three (compliance_pack / isolation_level / data_residency) currently just pass format validation and aren't read at runtime (sandbox isolation is always shared for now); the audit retention period is set platform-wide for now.",
    section_model: "Model",
    section_prompt: "System prompt",
    field_prompt_placeholder: "You are a helpful assistant.",
    section_tools: "Tools",
    tool_web_search: "Web search",
    tool_http: "HTTP tool",
    tool_mcp: "MCP tools",
    mcp_servers_label: "MCP servers this agent can use",
    mcp_server_search: "Search servers",
    mcp_tool_search: "Search tools",
    mcp_scope_label: "Tools:",
    mcp_scope_all: "All tools",
    mcp_scope_specific: "Specific",
    mcp_select_all: "Select all",
    mcp_clear: "Clear",
    mcp_selected_count: "{{count}} selected",
    mcp_choose_tools: "Choose tools",
    mcp_done: "Done",
    mcp_all_tools: "All tools",
    mcp_tools_count: "{{count}} tools selected",
    mcp_servers_hint: "Leave all unchecked to allow every available server",
    mcp_no_servers_available:
      "No MCP servers available yet. Enable one under Settings → MCP Servers → Add → enable from the catalog.",
    mcp_no_servers_catalog:
      "No published MCP connectors yet. Publish one under Settings → MCP Catalog.",
    mcp_source_platform: "platform",
    mcp_source_tenant: "tenant",
    mcp_tools_label: "Tools",
    mcp_tools_hint:
      "Leave all unchecked to allow every tool from the selected servers",
    mcp_tools_loading: "Loading tools…",
    mcp_tools_unreachable: "Could not load tools",
    mcp_servers_loading: "Loading servers…",
    mcp_servers_load_failed: "Could not load servers",
    section_reflection_evaluator: "Reflection evaluator model (optional)",
    reflection_evaluator_hint:
      "The model that scores answers during reflection; unset = the agent's own main model judges.",
    reflection_evaluator_clear: "Clear (use the agent's own model)",
    section_vision: "Image understanding (VL model)",
    vision_hint:
      "The main model can't read images.\nAdd a model that can, and the agent can read images (via the ask_image tool); reasoning still runs on the main model.",
    vision_clear: "Clear (no image understanding)",
    section_vision_help:
      "Shown only when the main model is text-only.\nThe vision model answers image-related questions; the main model coordinates.\nLeave empty = the agent can't see images.\nExample: qwen-vl-max",
    section_fallback: "Fallback models",
    section_fallback_help:
      "If the main model fails or times out, the run switches to the fallback models in order instead of failing.\nNone configured = the agent only uses its main model.\nTip: pick fallbacks from different vendors so one vendor's outage doesn't take down the whole chain.",
    fallback_hint:
      "If the main model fails or times out, the run switches to the fallback models in order.",
    fallback_empty: "No fallback models yet.",
    fallback_rank: "Fallback model {{n}}",
    fallback_add: "Add fallback model",
    fallback_remove: "Remove",
    field_name_help:
      "The agent's unique id that external apps call by.\nLowercase letters, digits, and hyphens only.\nExample: support-bot",
    field_description_help:
      "One line on what this agent does, shown to users.\nExample: Customer-support assistant for product questions",
    section_model_help:
      "The LLM powering the agent's main conversation.\nPick the provider, then the model; higher temperature = more varied.\nExample: anthropic / claude-sonnet-4-5, temperature 0.2",
    section_prompt_help:
      "System prompt — defines the agent's role, tone, and rules. The core of its persona.\nExample: You are a senior Python engineer; answer concisely with runnable code",
    section_reflection_evaluator_help:
      "Specifies which model judges reflection self-assessment. When unset, reflection reuses the main model.",
    section_defenses: "Defenses",
    section_defenses_help:
      "Configure this agent's safety posture: input injection defense, output screening/redaction, and tool-action review.",
    defenses_extends_note:
      "This agent extends a template. The template may enforce stricter defenses than shown here — switches you weaken here can be overridden by the template's floor.",
    defenses_group_input: "Input defense",
    defenses_group_output: "Output defense",
    defenses_group_action: "Tool-action defense",
    defenses_prompt_injection: "Injection spotlighting",
    defenses_prompt_injection_brief:
      "Flags outside content so it can't fool the model",
    defenses_prompt_injection_help:
      "Marks content from untrusted sources (retrieved data, tool output) to reduce prompt-injection hijack risk. On by default.",
    defenses_prompt_injection_off_warn:
      "Off: untrusted content is no longer marked, weakening injection defense.",
    defenses_output_screen: "Output rule screen",
    defenses_output_screen_brief: "Blocks replies that look like a leak",
    defenses_output_screen_help:
      "Rule-based blocking of replies that look like credential leaks / exfiltration. On by default; recommended.",
    defenses_output_screen_off_warn:
      "Off: credential-leak / exfil-shaped replies are no longer blocked (on by default; not recommended).",
    defenses_output_judge: "Model-backed output judge",
    defenses_output_judge_brief: "Has a model double-check each reply",
    defenses_output_judge_help:
      "Uses a model to judge each reply for alignment / leakage — a backstop above the rule screen.",
    defenses_output_judge_on_warn:
      "Adds one extra LLM call per reply (higher latency and cost); and disables token-by-token streaming for this agent (the reply is returned all at once). The judge model is configured in Platform Settings.",
    defenses_output_judge_on_error: "When the judge fails",
    defenses_output_dlp: "Output PII redaction",
    defenses_output_dlp_brief: "Hides personal info in replies",
    defenses_output_dlp_help:
      "Replaces PII (email / phone / national ID / card number) in replies with [redacted].",
    defenses_output_dlp_on_note:
      "Rewrites legitimate replies containing PII, e.g. \"your email is a@b.com\" → \"your email is [redacted]\".",
    defenses_action_screen: "Tool-call review",
    defenses_action_screen_brief: "Checks each tool call before it runs",
    defenses_action_screen_help:
      "Judges each tool call for alignment before it runs: off / block / route to human approval.",
    defenses_action_screen_off: "Off",
    defenses_action_screen_block: "Block",
    defenses_action_screen_approval: "Human approval",
    defenses_action_screen_on_note:
      "Adds one judgement before each tool call, increasing latency on tool turns (approval mode also pauses for a human).",
    defenses_action_screen_on_error: "When review fails",
    defenses_on_error_open: "Allow (fail-open)",
    defenses_on_error_closed: "Block (fail-closed)",
    section_tools_help:
      "Check the tools the agent may use.\nMore capability = more power, but harder to manage.\nExample: web search + MCP",
    section_mcp: "MCP",
    section_mcp_help:
      "Connect the agent to MCP servers (sources of external tools).\nPick servers, and optionally narrow to specific tools.\nExample: connect a maps service for location lookups",
    tool_web_search_help:
      "Let the agent search the web for fresh info — works out of the box (the platform's own free search service, no setup needed).\nExample: handy for 'what's in today's news' questions",
    tool_http_help:
      "Let the agent visit external URLs and call third-party APIs (every request is logged for audit).\nExample: checking the weather, calling your own internal services",
    tool_mcp_help:
      "Let the agent use tools provided by MCP servers (databases, business systems).\nOnce checked, pick the specific servers and tools below.\nExample: connect your company's CRM",
    tool_exec_python: "Run Python code",
    tool_exec_python_help:
      "Let the agent run Python in an isolated sandbox for calculations and data processing. On by default; can be turned off for purely conversational agents.",
    tool_bash: "Run shell commands",
    tool_bash_help:
      "Let the agent run shell commands in an isolated sandbox (e.g. installing dependencies, running scripts). On by default; can be turned off for agents that don't need to touch the system.",
    tool_manage_task: "Scheduled tasks",
    tool_manage_task_help:
      "Let the agent create scheduled tasks for the user from within the conversation (e.g. \"search news every day at 9am\") — it runs automatically at the scheduled time and the result comes back to the conversation.",
    tool_author_skill: "Create skills",
    tool_author_skill_help:
      "Let the agent save a reusable approach as a \"skill\" that it — or other agents — can use directly later.\nExample: save the fixed steps for \"compiling the weekly report\" as a skill, then trigger it with one line next time.",
    tool_refine_skill: "Refine skills",
    tool_refine_skill_help:
      "Let the agent improve the content of an existing skill.\nExample: a skill's steps are out of date, so let the agent update it itself.",
    tool_fork_skill: "Fork skills",
    tool_fork_skill_help:
      "Let the agent copy an existing skill and modify the copy, leaving the original untouched.\nExample: base a new \"daily report\" skill on the existing \"weekly report\" one.",
    tool_propose_skill: "Propose skill to team",
    tool_propose_skill_help:
      "Let the agent submit a skill it worked out for team-wide sharing (needs human approval).\nExample: the agent found a good approach and wants other agents on the team to use it too.",
    tool_note_behavior_patch: "Record behavior corrections",
    tool_note_behavior_patch_help:
      "Let the agent jot down small corrections — \"next time, handle this situation like so\" — to gradually improve its own performance.",
    tool_clarify_tool_usage: "Clarify tool usage",
    tool_clarify_tool_usage_help:
      "Let the agent record the correct way to use a tool after misusing it, so it doesn't repeat the mistake.",
    tools_config_note:
      "File read/write, saving artifacts, and asking for human approval are on by default and not listed here — edit the YAML view to change them. Web search works out of the box (the platform's own free search service).",
    section_approval: "Human approval",
    section_approval_help:
      "For the tools you check, execution pauses every time and waits for a human to approve — whether it's a base capability that's always on, or one you turned on yourself above in Tools.\nExample: add approval for exec_python, http",
    approval_hint: "Check the tools that need a human sign-off before running.",
    section_dynamic_workers: "Temporary helpers",
    section_dynamic_workers_help:
      "When on (the default), the agent can pull in a few temporary helpers mid-task to handle subtasks in parallel.\nTurn off when you need strictly one worker.\nExample: on for a research agent, off for a simple FAQ bot",
    dynamic_workers_hint:
      "Let the agent create temporary helpers at run time to share the load (on by default).",
    approval_timeout: "Approval wait limit (seconds)",
    approval_timeout_brief: "Auto-rejects if no one responds in time",
    approval_timeout_help:
      "How long a pending approval may wait (seconds) before it is auto-rejected, so it doesn't tie up resources.\nDefault 24h (86400).\nExample: 86400",
    section_knowledge: "Knowledge bases (RAG)",
    section_knowledge_help:
      "Knowledge bases the agent can search to back up its answers.\nPick existing bases, or type a name.\nExample: hr-policies, eng-handbook",
    knowledge_hint: "Knowledge bases this agent may search.",
    knowledge_placeholder: "Select or type knowledge base names",
    knowledge_chunk_label: "chunk {{max}}/overlap {{overlap}}",
    section_skills: "Skills",
    section_skills_help:
      "Skill packages (instructions + scripts) attached to the agent.\nPick from your tenant's and the platform's skills.\nExample: pptx-builder, sql-analyst",
    skills_hint:
      "Check the skills to attach to this agent. Platform skills your plan tier cannot use are not selectable.",
    auto_attach_evolved: "Auto-attach self-evolved skills",
    auto_attach_evolved_help:
      "When on, builds automatically attach this agent's own promoted (ACTIVE) distilled skills without editing the manifest.\nAttached lazily: a summary in <available-skills> only, never eager prompt content.\nSkills you check below always take precedence over an auto-attached duplicate.",
    skills_search: "Search skills (name / description / category)",
    skills_empty: "No skills available",
    skills_filter_category: "Filter by category",
    skills_filter_source: "Filter by source",
    skills_source_platform: "Platform",
    skills_source_tenant: "Tenant",
    skills_tier_locked: "Needs {{tier}}",
    skills_tier_locked_hint:
      "This platform skill requires the {{tier}} plan; the current tenant is not entitled.",
    section_subagents: "Sub-agent delegation",
    section_subagents_help:
      "Other deployed agents this one can hand a subtask to.\nEach target shows up to the agent as a tool it can call by name.\nExample: tool name 'researcher' → target deep-researcher@1.0.0",
    subagents_hint: "Deployed agents this agent may delegate subtasks to.",
    subagent_name: "Tool name",
    subagent_ref: "Target agent",
    subagent_description: "When to delegate",
    subagent_remove: "Remove",
    subagent_add: "Add sub-agent",
    section_prompt_vars: "Dynamic prompt (Jinja)",
    section_prompt_vars_help:
      "Declare the variables used in the prompt.\nValues come in through the API's 'inputs' on each call and are filled in per run; only names declared here work.\nTurning 'Trusted' off treats the incoming value as plain data, preventing any instructions hidden in it from running (use for variables that may carry user input).\nExample: write {{ persona }} in the template, declare variable 'persona' here",
    prompt_jinja_label: "Enable Jinja templating",
    prompt_jinja_hint:
      "When on, the prompt body is a Jinja template: {{ name }} placeholders are filled on each call from the API's 'inputs'.\nOnly the body you write is rendered.",
    prompt_var_name: "Variable name",
    prompt_var_trusted: "Trusted",
    prompt_var_required: "Required",
    prompt_var_description: "Description",
    prompt_var_remove: "Remove",
    prompt_var_add: "Add variable",
    section_output_schema: "Structured output",
    output_schema: {
      on_label: "Reply using a template",
      hint_off:
        "Off: free-text replies. Turn it on to have the agent reply with a fixed set of fields (e.g. a support ticket: title + severity + summary) - easier to wire up to other programs or spreadsheets.",
      col_name: "Field name",
      col_type: "Type",
      col_required: "Required",
      col_desc: "Description",
      type_string: "Text",
      type_number: "Number",
      type_integer: "Integer",
      type_boolean: "Yes/No",
      type_array_string: "Text list",
      type_array_number: "Number list",
      add_field: "Add field",
      remove_field: "Remove",
      name_invalid:
        "Field names must start with a letter or underscore, and contain only letters, numbers, and underscores.",
      name_duplicate: "Duplicate field name - a field above already uses this name.",
      complex_readonly: "Configured (complex structure) - edit it in the YAML view.",
      off_confirm: "Turning this off will clear the reply template you've defined. Continue?",
    },
    inject_date_label: "Inject current date",
    inject_date_hint:
      "Writes today's date into the system prompt at build time (on by default, cache-stable per calendar day). When off, the agent doesn't know today's date - only suitable for agents whose behavior doesn't depend on the date.",
    dynamic_context_note:
      "Advanced items like custom reminders (dynamic_context.custom_reminders) need the YAML view.",
  },
  playground: {
    session_label: "Session",
    new_session: "New session",
    session_failed: "Failed to create session",
    thread_id: "thread",
    input_placeholder:
      "Type a prompt to send to the agent. The full SSE event stream lands on the right.",
    run: "Run",
    running: "Running…",
    stop: "Stop",
    event_log: "Event log",
    event_count: "{{n}} events",
    stream_failed: "Stream failed",
    empty_log: "No events yet — click Run to start.",
    attach_image: "Attach image",
    attach_document: "Attach document",
    uploaded_docs_note: "[Uploaded documents — read them with read_document]",
    prompt_vars_label: "Prompt variables",
    attachments_label: "Attachments",
    remove_attachment: "Remove attachment",
    upload_failed: "Image upload failed",
    workspace_full: "Workspace is full — delete some files and retry",
    uploading: "Uploading…",
    transcript_label: "Conversation",
    turn_count: "{{n}} turns",
    turn_running: "Running…",
    turn_no_text: "(no text answer)",
    segment_commentary: "Progress note",
    turn_failed: "This turn's run failed",
    usage_in: "in",
    usage_out: "out",
    usage_total: "total",
    usage_cache: "cached",
    usage_reasoning: "reasoning",
    usage_cache_write: "Cache write",
    meta_finish: "Finish",
    meta_model: "Model",
    reasoning_label: "Reasoning",
    view_full_text: "View full text",
    copy_all: "Copy all",
    copy_done: "Copied to clipboard",
    copy_failed: "Copy failed — select the text manually",
    events_label: "Events",
    export_json: "Export JSON",
    export_json_tip:
      "Export this turn's full event stream (authoritative backend record) as JSON",
    retry: "Retry",
    workspace_label: "Workspace",
    workspace_refresh: "Refresh workspace",
    workspace_volume: "Volume",
    workspace_size: "Size",
    workspace_deleted: "deleted",
    workspace_none: "No workspace yet — runs that write files create it.",
    workspace_artifacts: "Artifacts",
    workspace_files: "Files",
    workspace_file_download: "Download {{name}}",
    artifact_download: "Download artifact {{name}}",
    artifact_delete: "Delete artifact {{name}}",
    artifact_delete_confirm:
      "Delete this artifact? Unregisters it only; the underlying file stays.",
    file_delete: "Delete file {{name}}",
    file_delete_confirm: "Permanently delete this file? This cannot be undone.",
    delete_ok: "Delete",
    delete_cancel: "Cancel",
    resume_label: "Resume…",
    resumed_notice:
      "Continuing an existing thread — prior conversation loaded below.",
    history_divider: "— new messages below —",
    history_loading: "Loading debug data…",
    meta_steps: "steps",
    meta_latency: "latency",
    view_run: "View run",
    feedback_up: "Good answer",
    feedback_down: "Bad answer",
    feedback_comment_placeholder: "What went wrong? (optional — helps the agent improve)",
    feedback_comment_submit: "Submit feedback",
    feedback_thanks: "Thanks for the feedback",
    feedback_failed: "Feedback failed",
    approval_awaiting: "awaiting approval",
    approval_approve: "Approve",
    approval_reject: "Reject",
    approval_modify_hint: "Edit arguments on the run-detail page.",
    history_button: "History",
    tool_count_one: "{{count}} tool",
    tool_count_other: "{{count}} tools",
    tool_failed_count: "{{count}} failed",
    state_memories: "Recalled memory",
    state_failures: "Tool failures",
    state_reflections: "Reflections",
    state_subagents: "Subagent calls",
    state_signals: "Run signals",
    state_retries: "Retries",
    state_per_step: "Per-step tokens",
    signal_no_progress: "No-progress streak",
    signal_escalate: "Escalate next",
    retry_attempt: "attempt {{n}} · {{cls}} · backoff {{s}}s",
    state_section: "Run state",
    tl_step: "Step {{n}}",
    streaming_badge: "streaming…",
    interrupted_badge: "interrupted",
    ttft: "TTFT {{d}}",
    streaming_reasoning_label: "Thinking…",
    reasoning_summary: "Thought for {{d}}",
    tl_finish: "finish: {{reason}}",
    tl_tool_failed: "Tool failed → retry",
    tl_final_answer: "Final answer",
    tl_step_output: "Step output",
    tl_reasoning: "Reasoning (this LLM call)",
    tl_expand: "Expand",
    tl_critique: "critique",
    tl_legend_agent: "agent (LLM step: reasoning + tool / answer)",
    tl_legend_mem: "memory_recall / writeback",
    tl_legend_plan: "planner",
    tl_legend_reflect: "reflect (accept/revise)",
    tl_legend_marker: "compaction / retry / error",
    tl_legend_approval: "approval (paused for review)",
    tl_importance: "importance {{v}}",
    tl_confidence: "confidence {{v}}",
    tl_goal: "Goal: {{text}}",
    tl_duration: "step duration",
    tr_col_action: "Action",
    tr_state_not_ready_title: "Processing",
    tr_state_not_ready_msg:
      "The run just finished — the trace is still being ingested (about 1 second). Refresh shortly to see exact timings.",
    tr_state_unavailable_title: "Unavailable",
    tr_state_unavailable_msg:
      "The trace service is temporarily unreachable. Try again later for exact timings — the rest of this run's debug data is unaffected.",
    tr_state_no_trace_title: "No trace",
    tr_state_no_trace_msg:
      "This run has no associated trace (Langfuse wasn't enabled, or it's an older run).",
    tr_detail_close: "Close",
    rb_ok: "Succeeded",
    rb_failed_at: "Failed at {{label}}",
    rb_jump: "Jump to error ↓",
    tr_detail_latency: "duration",
    tr_detail_model: "model",
    tr_detail_tokens: "tokens",
    tr_detail_cost: "cost",
    tr_io_llm_msgs: "Messages",
    tr_io_llm_msgs_hint: "The full context sent to the model",
    tr_io_llm_out: "Reply",
    tr_io_llm_out_hint: "The model's output",
    tr_io_tool_args: "Arguments",
    tr_io_tool_args_hint: "The input passed to the tool",
    tr_io_tool_result: "Result",
    tr_io_tool_result_hint: "What the tool returned",
    tr_io_in: "Input",
    tr_io_out: "Output",
    tr_msg_truncated: "Truncated {{n}} chars",
    tr_msg_copy: "Copy",
    tr_msg_raw: "View raw",
    tr_msg_untrusted: "contains untrusted content",
    tr_msg_toolcall: "→ called {{name}}",
    tr_msg_chars: "{{n}} chars",
    tr_purpose_primary: "Primary reasoning",
    tr_aux_llm: "Auxiliary LLM call",
    tr_raw_error: "Failed to load raw content",
    tl_worker_running: "running",
    tl_worker_success: "done",
    tl_worker_max_steps: "step limit",
    tl_worker_cancelled: "cancelled",
    tl_worker_task: "Task",
    tl_worker_summary: "{{steps}} steps · {{calls}} LLM calls · {{duration}}",
    tl_worker_children: "child workers",
    task_result: "Task result",
    lifecycle_created: "Created",
    lifecycle_fired: "Fired",
    lifecycle_completed: "Completed",
    fire_pending:
      "The result hasn't landed yet — the run is still going. Reopen this conversation later to see it.",
    gantt_expand: "Expand",
    gantt_degraded: "Timeline approximated by event order (server timestamps unavailable)",
  },
  session_history: {
    title: "Session history",
    search_placeholder: "Search title…",
    filter_status: "Filter by status",
    filter_all: "All",
    empty: "No sessions yet — send a message to start.",
    empty_search: "No matching sessions.",
    load_more: "Load more",
    rename: "Rename",
    rename_placeholder: "Session title",
    rename_ok_button: "Save",
    rename_ok: "Renamed",
    archive: "Archive",
    archive_confirm: "Archive this session? Hidden from the list, reversible.",
    archive_ok: "Archived",
    purge: "Delete forever",
    purge_confirm: "Permanently delete this session?",
    purge_warning: "Irreversible — the conversation and run records are wiped.",
    purge_ok: "Deleted",
    cancel: "Cancel",
    action_failed: "Action failed",
    time_now: "just now",
    time_minutes: "{{n}}m ago",
    time_hours: "{{n}}h ago",
    time_days: "{{n}}d ago",
    status_active: "Active",
    status_paused: "Paused",
    status_completed: "Completed",
    status_failed: "Failed",
    status_cancelled: "Cancelled",
    status_archived: "Archived",
  },
  event_stream: {
    title: "Event stream",
    connecting: "Connecting…",
    event_count: "{{n}} events",
    stream_failed: "Stream failed",
    empty: "No events yet.",
    view_timeline: "Tool calls",
    view_raw: "Raw events",
    view_exact: "Execution trace",
    compaction_label: "Context compacted",
    compaction_passes: "{{n}} pass(es)",
    compaction_reduction: "{{before}} → {{after}} tokens (−{{pct}}%)",
    compaction_summary_chars: "summary {{n}} chars",
  },
  tool_timeline: {
    empty: "No tool calls yet.",
    mcp_badge: "MCP",
    builtin_badge: "builtin",
    status_pending: "running",
    status_success: "ok",
    status_error: "failed",
    status_pending_approval: "awaiting approval",
    args_label: "Arguments",
    result_label: "Result",
    exit_code: "Exit code",
    stdout_label: "stdout",
    stderr_label: "stderr",
    timed_out: "timed out",
    fire_now: "Run now",
    firing: "Running…",
    fire_delivered: "Result delivered",
    fire_pending: "Fired, still running",
    fire_failed: "Fire failed",
  },
  approval_card: {
    awaiting_human: "awaiting approval",
    reason_kind: "Reason",
    requested_at: "Requested",
    timeout_at: "Timeout",
    proposed_args_label: "Proposed arguments (read-only)",
    editing_hint: "Editing arguments — Approve sends them as 'modify' decision",
    edit_arguments: "Edit arguments",
    cancel_edit: "Cancel edit",
    approve: "Approve",
    approve_with_edits: "Approve with edits",
    reject: "Reject",
    approved: "Approved — run resuming.",
    approved_with_edits: "Approved with edits — run resuming.",
    rejected: "Rejected — run cancelled.",
    json_parse_error: "Invalid JSON",
    json_must_be_object: "Top-level value must be a JSON object",
    binding: "Binding",
    binding_hint:
      "Fingerprint of the approved arguments, bound at request and re-verified before execution. If the checkpointed call drifts from this, the resume is rejected.",
    workspace_drift_title: "Workspace changed since this was requested",
    workspace_drift_body:
      "A write-capable tool (write_file / edit_file / bash) ran in this user's workspace after the approval was requested. Any files the approved command reads may differ from what you're reviewing. This is a forensic signal and does not block execution.",
  },
  plan_panel: {
    title: "Plan",
    progress: "{{done}}/{{total}} completed",
    edit: "Edit plan",
    cancel: "Cancel",
    save: "Save plan",
    saved: "Plan saved — the workspace PLAN.md syncs on the next turn.",
    locked_while_running: "The agent owns the plan while a run is live.",
    goal_placeholder: "Goal — one sentence",
    step_placeholder: "Step description",
    add_step: "Add step",
    remove_step: "Remove step",
    no_plan: "The agent has not made a plan yet.",
    status_pending: "pending",
    status_in_progress: "in progress",
    status_completed: "completed",
  },
  run_detail: {
    failed_to_load: "Failed to load run",
    back_to_conversation: "Conversation",
    thread_label: "Thread",
    awaiting_approval: "awaiting approval",
    reason_kind: "Reason",
    requested_at: "Requested",
    timeout_at: "Timeout",
    proposed_args: "Proposed arguments",
    approve: "Approve",
    reject: "Reject",
    approved: "Approved — run resuming.",
    rejected: "Rejected — run cancelled.",
    run_metadata: "Run metadata",
    run_id: "Run ID",
    thread_id: "Thread ID",
    status: "Status",
  },
  trace_toolbar: {
    title: "Trace",
    no_trace: "No trace recorded for this run.",
    copy_aria: "Copy trace ID",
    copied: "Trace ID copied",
    open_in_langfuse: "Open in Langfuse",
    langfuse_unconfigured_hint:
      "Set VITE_LANGFUSE_BASE_URL to enable the deep link.",
  },
  trace: {
    breakdown_title: "Entry chain + first generation {{d}}",
  },
  approval_badge: {
    aria_label: "Pending approvals",
    tooltip_one: "{{count}} run is awaiting approval",
    tooltip_other: "{{count}} runs are awaiting approval",
  },
  curation: {
    page_title: "Curation+Eval",
    subtitle:
      "Review candidate runs surfaced by the curation worker and promote them to golden eval datasets.",
    tab_candidates: "Candidates",
    tab_datasets: "Eval Datasets",
    cross_tenant_banner: "cross-tenant view",
    failed_to_load: "Failed to load candidates",
    empty_home: "No pending candidates in this tenant.",
    empty_cross: "No candidates across all tenants.",
    col_signal: "Signal",
    col_agent: "Agent",
    col_status: "Status",
    col_detected: "Detected",
    col_outcome: "Outcome",
    filter_status: "Status filter",
    filter_status_all: "All statuses",
    filter_signal: "Signal filter",
    filter_signal_all: "All signals",
    detail_title: "Candidate detail",
    detail_signal: "Signal",
    detail_outcome: "Outcome",
    detail_trajectory: "Trajectory",
    trajectory_missing:
      "Trajectory artifact missing — promote / dismiss still allowed.",
    promote: "Promote",
    dismiss: "Dismiss",
    promote_modal_title: "Promote candidate to eval dataset",
    promote_dataset_name: "Eval dataset name",
    promote_name_required: "Name is required",
    promote_hint:
      "Trajectory input + expected output are copied from the candidate. Edit later via the Eval Datasets tab.",
    promoted: "Candidate promoted to eval dataset.",
    dismissed: "Candidate dismissed.",
  },
  audit: {
    page_title: "Audit",
    subtitle:
      "Immutable trail of every mutating action. Cross-tenant view requires system_admin. Payloads are PII-redacted at write time.",
    cross_tenant_banner: "cross-tenant view",
    failed_to_load: "Failed to load audit log",
    empty: "No audit entries match these filters.",
    filter_actor: "Actor ID (exact)",
    filter_action: "Action (e.g. memory:update)",
    filter_resource_type: "Resource type",
    filter_result: "Result",
    filter_result_all: "All results",
    load_more: "Load more",
    detail_title: "Audit entry detail",
    detail_id: "ID",
    detail_occurred_at: "Occurred at",
    detail_actor: "Actor",
    detail_on_behalf_of: "On behalf of",
    detail_action: "Action",
    detail_resource: "Resource",
    detail_result: "Result",
    detail_reason: "Reason",
    detail_trace_id: "Trace ID",
    detail_ip: "IP",
    detail_payload: "Payload (details)",
    detail_payload_hint:
      "Already redactor-cleaned at write time — sensitive fields surface as [REDACTED].",
    mcp_traffic_hint: "MCP traffic to {{server}} — server · response size",
  },
  egress_audit: {
    page_title: "Egress audit",
    subtitle:
      "Every sandbox→internet connection through the audited egress proxy — host, port, byte volumes, verdict. Payloads are never recorded (HTTPS is tunnelled). Cross-tenant view requires system_admin.",
    cross_tenant_banner: "cross-tenant view",
    load_error: "Failed to load egress audit",
    empty: "No egress connections match these filters.",
    filter_verdict: "Verdict",
    filter_host: "Target host",
    filter_agent: "Agent name",
    load_more: "Load more",
    detail_title: "Egress connection detail",
    col_verdict: "Verdict",
    col_host: "Target",
    col_agent: "Agent",
    col_bytes_up: "Bytes up",
    col_bytes_down: "Bytes down",
    col_duration: "Duration",
    col_sandbox: "Sandbox",
    col_error: "Error",
    col_time: "Occurred at",
  },
  settings_ops: {
    quotas_page_title: "Tenant Quotas",
    quotas_subtitle:
      "Per-tenant rate / cost limits keyed by dimension. Tenant-scoped only — switch tenant via the top-bar to manage another.",
    config_page_title: "Tenant Config",
    config_subtitle:
      "Per-tenant feature knobs: plan, retention, MCP/HTTP allowlists, PII fields. Same per-tenant scope as Quotas.",
    config_record_title: "Current config",
    config_edit_title: "Edit config (JSON patch)",
    config_not_found:
      "No tenant_config row exists for this tenant yet. First save will create one.",
    cross_tenant_blocked_title: "Cross-tenant view does not apply here",
    cross_tenant_blocked_body:
      "Quotas + config are managed per-tenant. Switch to a specific tenant via the top-bar to view / edit.",
    failed_to_load: "Failed to load",
    quota_create: "Create Quota",
    quota_create_modal_title: "Create / upsert quota",
    quota_created: "Quota saved.",
    quota_deleted: "Quota deleted.",
    quota_empty: "No quotas configured for this tenant.",
    quota_delete_confirm_title: "Delete this quota?",
    quota_delete_confirm_body:
      "The dimension will fall back to platform defaults until re-created.",
    col_dimension: "Dimension",
    col_limit_value: "Limit",
    col_burst: "Burst",
    col_scope: "Scope",
    col_updated: "Updated",
    col_actions: "Actions",
    field_dimension: "Dimension",
    field_limit_value: "Limit value",
    field_burst: "Burst (optional)",
    burst_hint:
      "Token-bucket burst capacity. Defaults to platform setting when empty.",
    dimension_required: "Dimension is required",
    limit_required: "Limit value is required",
    display_name: "Display name",
    plan: "Plan",
    audit_retention_days: "Audit retention (days)",
    event_log_retention_days: "Event log retention (days)",
    rate_limit_override: "Per-tenant rate limit",
    rate_limit_default: "Platform default",
    skill_stale_days: "Skill stale threshold (days)",
    skill_archive_days: "Skill archive threshold (days)",
    skill_evolution_enabled: "Skill self-evolution",
    skill_evolution_on: "Enrolled",
    skill_evolution_off: "Not enrolled",
    skill_evolution_judge_sample_pct: "Implicit judge sample rate",
    mcp_allowlist: "MCP allowlist",
    http_allowlist: "HTTP tool allowlist",
    http_allowlist_empty: "— (all public hosts allowed; SSRF blocked)",
    http_denylist: "HTTP tool denylist",
    http_denylist_empty: "— (nothing denied)",
    updated: "Updated",
    dirty: "edited (unsaved)",
    config_saved: "Tenant config saved.",
    config_parse_error: "JSON parse error",
    config_etag_hint_title: "Last-writer-wins (M0)",
    config_etag_hint_body:
      "No ETag concurrency check yet — M1 will add If-Match. If another admin is editing simultaneously, the last Save will overwrite. Reload after save to see latest.",
  },
  settings_credentials: {
    page_title: "Credentials",
    subtitle:
      "LLM and tool credentials are platform-managed. This read-only view shows, per provider / tool, whether the platform has a credential configured and how many of this tenant's agents use it.",
    failed_to_load: "Failed to load credentials",
    mode_label: "Credentials mode",
    mode_platform: "Platform",
    mode_help_platform:
      "All LLM / tool calls use the platform's credentials. Credentials are managed at the platform level.",
    providers_heading: "Provider credentials",
    tools_heading: "Tool credentials",
    col_provider: "Provider",
    col_tool: "Tool",
    col_platform_status: "Platform status",
    col_used_by: "Used by (agents)",
    status_configured: "Configured",
    status_not_set: "Not configured",
    empty: "No entries — the platform has opted into no providers / tools yet.",
  },
  settings_tenants: {
    page_title: "Tenants",
    subtitle:
      "All tenants on the platform. Click Manage to switch into a tenant and edit its config, quotas, and credentials.",
    not_admin_title: "System admin only",
    not_admin_body:
      "Listing all tenants is a platform-level action available to system admins.",
    col_display_name: "Display name",
    col_plan: "Plan",
    col_tenant_id: "Tenant id",
    col_created: "Created",
    col_actions: "Actions",
    col_status: "Status",
    st_active: "Active",
    st_suspended: "Suspended",
    deactivate: "Deactivate",
    activate: "Activate",
    deactivate_confirm:
      "Suspend this tenant? Its members will be blocked until reactivated.",
    status_change_failed: "Failed to change tenant status",
    status_changed: "Tenant status updated.",
    manage: "Manage",
    failed_to_load: "Failed to load tenants",
    empty: "No tenants yet — create one from Create Tenant.",
    create: "Create Tenant",
  },
  settings_create_tenant: {
    page_title: "Create Tenant",
    subtitle:
      "Provision a new tenant. Platform-level action — system admins only. The new tenant id is shown on success so you can configure it.",
    not_admin_title: "System admin only",
    not_admin_body:
      "Creating tenants is a platform-level operation. Ask a system admin to provision one.",
    field_display_name: "Display name",
    display_name_required: "Display name is required",
    field_plan: "Plan",
    field_tenant_id: "Tenant id (optional)",
    tenant_id_hint:
      "Leave empty to let the server generate a UUID. Supply one only for idempotent provisioning.",
    tenant_id_placeholder: "auto-generated",
    tenant_id_invalid:
      "Must be a valid UUID (e.g. 123e4567-e89b-12d3-a456-426614174000), or leave empty to auto-generate.",
    create_btn: "Create tenant",
    created: "Tenant created.",
    created_detail: "New tenant id:",
    field_first_admin_email: "First admin email (optional)",
    field_first_admin_display_name: "First admin display name (optional)",
    first_admin_hint:
      "Provisions the company's first admin and sends a set-password email. In dev (no SMTP) set the password in the Keycloak admin console instead.",
    first_admin_email_invalid: "Enter a valid email address",
    first_admin_provisioned: "First admin invited:",
  },
  mcp_servers: {
    page_title: "MCP Servers",
    subtitle: "Manage the remote MCP servers your agents can call tools from",
    add: "Add MCP server",
    col_name: "Name",
    col_transport: "Transport",
    col_url: "URL",
    col_auth: "Auth",
    col_status: "Status",
    col_tools: "Tools",
    col_tenant: "Tenant",
    col_actions: "Actions",
    status_enabled: "Running",
    status_disabled: "Disabled",
    test: "Test",
    edit: "Edit",
    delete: "Delete",
    testing: "Testing…",
    connected: "Connected · {{count}} tools",
    unreachable: "Unreachable",
    tools_loading: "Loading tools…",
    no_tools: "No tools advertised",
    empty_title: "No MCP servers yet",
    empty_hint:
      "MCP servers let your agents call external tools like GitHub or Linear.",
    delete_confirm: "Delete server {{name}}?",
    failed_to_load: "Failed to load MCP servers",
    source_platform: "Platform",
    source_custom: "Custom",
    platform_hosted: "Platform-hosted",
    status_enabled_platform: "Enabled (platform)",
    needs_authorize: "Needs your authorization",
    remove: "Remove",
    remove_confirm: "Remove platform server {{name}}?",
    remove_unavailable: "This platform server can't be removed (no catalog reference).",
    act_run: "Run",
    act_stop: "Stop",
  },
  create_mcp_server: {
    add_title: "Add MCP server",
    edit_title: "Edit MCP server",
    field_name: "Name",
    field_transport: "Transport",
    field_url: "URL",
    field_auth: "Auth type",
    field_token: "Bearer token",
    field_timeout: "Timeout (seconds)",
    token_hint_create: "Pasted once, stored encrypted — never shown again",
    token_hint_edit:
      "Leave blank to keep the current token; enter a new value to rotate",
    test_connection: "Test connection",
    test_ok: "Connected · {{count}} tools",
    test_failed: "Connection failed",
    name_required: "Name is required",
    url_required: "URL is required",
    url_invalid: "URL must start with http:// or https://",
    token_required: "Token is required for bearer auth",
    submit_add: "Add",
    submit_save: "Save",
    custom_disabled:
      "Custom servers are disabled by your platform admin; use the catalog.",
    tab_basic: "Auth",
    tab_headers: "Headers",
    tab_config: "Config",
    headers_hint: "Extra HTTP headers sent to the MCP server (e.g. X-API-Key).",
    headers_hint_edit:
      "Header values are write-only — re-enter a value to change a header; leave blank to keep all current values.",
    header_name: "Header name",
    header_value: "Header value",
    header_value_keep: "Leave blank to keep",
    header_add: "Add header",
    header_name_invalid: "Header name may use letters, digits and hyphens only",
    field_sse_read_timeout: "SSE read timeout (seconds)",
    sse_read_timeout_hint:
      "Per-read wait for SSE streams. Leave blank for the default (300s).",
    field_name_help:
      "A unique name for this MCP server within your tenant; agents reference it by this name.\nExample: company-crm",
    field_transport_help:
      "How the client connects to the MCP server. SSE / streamable-http for remote servers.\nExample: sse",
    field_url_help:
      "The MCP server's endpoint URL. Must be reachable from the platform (private IPs are blocked).\nExample: https://mcp.example.com/sse",
    field_auth_help:
      "How to authenticate to the server. none = open; bearer = a static token; oauth2 = per-user login.\nExample: bearer",
    field_token_help:
      "The bearer token sent as Authorization. Stored encrypted; leave blank on edit to keep the existing one.\nExample: sk-xxxx (paste the real token)",
    field_timeout_help:
      "Max seconds to wait for a single MCP request before giving up.\nExample: 30",
    field_sse_read_timeout_help:
      "Max seconds to wait between chunks on an SSE stream. Leave blank for the default (300s).\nExample: 300",
  },
  mcp_catalog: {
    page_title: "MCP Catalog",
    subtitle:
      "Curate the MCP connector types tenants can add. Platform-level, system admins only.",
    add: "New connector",
    not_admin_title: "System admin only",
    not_admin_body:
      "The MCP connector catalog is managed by system admins. Ask one to add connectors.",
    failed_to_load: "Failed to load the MCP catalog",
    col_name: "Connector",
    col_category: "Category",
    col_transport: "Transport",
    col_tier: "Required plan",
    col_enabled: "Publish status",
    col_actions: "Actions",
    empty_title: "No connectors yet",
    empty_hint:
      "Add connector types so tenants can wire up GitHub, Linear and other MCP servers in a few clicks.",
    delete_confirm: "Delete connector {{name}}?",
    delete_in_use: "In use by tenants — cannot delete. Disable it instead.",
    deleted: "Connector deleted",
    tier_free: "Free",
    tier_pro: "Pro",
    tier_enterprise: "Enterprise",
    auth_none: "None",
    auth_bearer: "Bearer token",
    auth_bearer_shared: "Bearer (shared)",
    auth_oauth2: "OAuth (per-user)",
    tab_basic: "Basic",
    tab_auth: "Auth",
    tab_advanced: "Advanced",
    field_url: "Server URL",
    url_hint: "The concrete MCP server endpoint URL.",
    field_bearer_token: "Platform bearer token",
    bearer_token_required: "A bearer token is required for shared bearer auth",
    bearer_token_keep_hint: "Leave blank to keep the stored token",
    shared_bearer_warning:
      "Shared identity: every enabling tenant/user uses this one token. Use only for tools without per-user/tenant data isolation — for GitHub, business systems, etc. use OAuth.",
    field_oauth_client_id: "OAuth client ID",
    oauth_client_id_required: "OAuth client ID is required",
    field_oauth_scopes: "OAuth scopes",
    oauth_hint:
      "Each user authorizes their own account; register an OAuth app and supply its client ID.",
    add_title: "New connector",
    edit_title: "Edit connector",
    submit_add: "Create",
    submit_save: "Save",
    field_identifier: "Identifier",
    field_name_hint: "Lowercase identifier, immutable after creation",
    field_display_name: "Display name",
    field_description: "Description",
    field_category: "Category",
    category_placeholder: "Select a category…",
    cat_search: "Search & Retrieval",
    cat_database: "Database & Files",
    cat_payment: "Payment & Transactions",
    cat_location: "Location Services",
    cat_social: "Social Media",
    cat_design: "Design & Creative",
    cat_document: "Document Tools",
    cat_browser: "Browser Automation",
    cat_scraping: "Web Scraping",
    cat_dev_tools: "Developer Tools",
    cat_other: "Other",
    field_icon: "Icon",
    icon_hint: "Upload a square image (PNG/SVG/JPG), up to 32 KB.",
    icon_upload: "Upload icon",
    icon_clear: "Remove icon",
    icon_type_error: "Please choose an image file.",
    icon_too_large: "Icon must be 32 KB or smaller.",
    field_transport: "Transport",
    field_auth: "Auth type",
    field_timeout: "Timeout (seconds)",
    timeout_hint:
      "Connect/call cap — fail fast when the server is unreachable. Leave blank for the default (30s).",
    field_sse_timeout: "SSE read timeout (seconds)",
    sse_timeout_hint:
      "Max idle wait between streamed events — tolerate long-running tool calls. Leave blank for the default (300s).",
    field_required_tier: "Required plan",
    field_enabled: "Enabled",
    name_required: "A valid lowercase slug is required",
    display_name_required: "Display name is required",
    url_template_required: "URL template is required",
    browser_title: "Add MCP server",
    browser_empty: "No servers are available for your plan yet.",
    browser_failed: "Failed to load the catalog",
    locked_ribbon: "Locked",
    requires_tier: "Requires {{tier}} plan",
    advanced_hint: "Need something not in the catalog?",
    advanced_custom: "Advanced — add a custom server",
    back: "Back",
    err_tier_required:
      "Your plan does not include this connector. Upgrade to add it.",
    err_not_found: "This connector no longer exists.",
    oauth_badge: "OAuth",
    enable: "Enable",
    enabled: "Enabled",
    enable_aria: "Enable {{name}} for this tenant",
    authorize: "Authorize",
    shared_hint: "Shared identity — all enabled users share one credential.",
    tools_title: "Tools",
    tools_none: "Connected, but the server exposes no tools.",
    tools_unreachable: "Could not connect to the server.",
    tools_oauth_note:
      "OAuth connectors authorize per user, so tools can't be listed from here.",
    tab_config: "Config",
    tools_refresh: "Refresh",
    param_name: "Parameter",
    param_type: "Type",
    param_desc: "Description",
    tool_enable_aria: "Enable tool {{name}}",
    tool_no_params: "No parameters.",
  },
  mcp_oauth: {
    page_title: "My MCP Authorizations",
    page_subtitle: "MCP servers you've authorized with your own account.",
    failed_to_load: "Failed to load your connections",
    empty: "You haven't authorized any MCP servers yet.",
    col_connector: "MCP server",
    col_status: "Status",
    col_scopes: "Scopes",
    col_expires: "Token expires",
    col_actions: "Actions",
    status_pending: "Pending",
    status_connected: "Connected",
    status_expired: "Expired",
    status_revoked: "Revoked",
    status_error: "Error",
    has_error: "error",
    reauthorize: "Re-authorize",
    reauthorize_failed: "Could not start re-authorization",
    disconnect: "Disconnect",
    disconnect_confirm:
      "Disconnect this connector? Your stored token will be revoked.",
    disconnect_failed: "Could not disconnect",
    disconnected: "Disconnected",
    connect_title: "Connect {{name}}",
    connect_hint:
      "You'll be redirected to sign in and authorize with your own account. The agent then accesses only your data.",
    authorize: "Authorize",
    err_not_oauth: "This connector is not an OAuth connector.",
    err_not_configured:
      "OAuth is not configured on this deployment. Contact your administrator.",
    callback_title: "Connecting…",
    callback_exchanging: "Finishing authorization, one moment…",
    callback_failed: "Authorization failed",
    callback_back: "Back to my connections",
    callback_missing_params:
      "The callback is missing its state or code parameter.",
  },
  settings_platform: {
    page_title: "Platform Credentials",
    subtitle:
      "Platform-level provider & tool credential refs (the runtime overlay over env config). System admins only. DB rows win over env; disable to turn one off without deleting.",
    not_admin_title: "System admin only",
    not_admin_body:
      "Platform credentials are managed by system admins. Ask one to configure providers/tools.",
    failed_to_load: "Failed to load platform credentials.",
    tab_credentials: "Credentials",
    tab_models: "Model services",
    tab_cost: "Cost",
    tab_quality: "Quality",
    providers_heading: "Providers",
    tools_heading: "Tools",
    col_name: "Name",
    col_source: "Source",
    col_secret_ref: "Secret reference",
    col_enabled: "Enabled",
    col_used_by: "Used by agents",
    col_actions: "Actions",
    source_env: "env",
    source_db: "db",
    source_unset: "unset",
    edit_btn: "Edit",
    delete_confirm:
      "Delete this DB-managed credential? (env fallback, if any, applies again.)",
    edit_modal_title: "Edit {{key}} credential",
    secret_ref_label: "Secret reference (KMS / secret URI)",
    secret_ref_hint:
      "A secret manager reference (e.g. kms://platform/anthropic-key) — never a plaintext key.",
    mode_label: "Credential source",
    mode_value: "Paste a key",
    mode_ref: "Reference (URI)",
    value_label: "API key",
    value_hint:
      "Pasted once and encrypted at rest — it is never shown again or stored in plaintext.",
    value_required: "Paste a key, or switch to Reference.",
    enabled_label: "Enabled",
    saved: "Credential saved.",
    deleted: "Credential deleted.",
    unset_ref: "not set",
    col_keys: "Keys",
    col_key_id: "Key ID",
    col_priority: "Priority",
    keys_summary: "{{on}}/{{total}} on",
    add_key_btn: "Add key",
    add_key_modal_title: "Add a key to {{key}}",
    key_id_label: "Key ID",
    key_id_hint:
      "Distinguishes sibling keys of one provider (e.g. acct-b); 'default' is the primary.",
    key_id_invalid: "Letters, digits, dot, underscore, hyphen only.",
    priority_label: "Priority",
    priority_hint:
      "Lower is tried first; on rate-limit / out-of-balance / revoked the next key takes over.",
    delete_key_confirm:
      "Delete this key? (Sibling keys of the same provider still fail over.)",
    col_tenant_overrides: "Tenant overrides",
    tenant_overrides_btn: "Tenant overrides",
    tenant_drawer_title: "Per-tenant credential overrides",
    tenant_drawer_hint:
      "Overrides are platform-procured and platform-managed; tenants never see them. A disabled override suppresses the key for the tenant (no fallback).",
    tenant_select_label: "Tenant",
    tenant_select_placeholder: "Select a tenant…",
    col_effective: "Effective",
    tenant_source_tenant: "override",
    tenant_source_suppressed: "suppressed",
    tenant_source_db: "platform (db)",
    tenant_source_env: "platform (env)",
    tenant_source_unset: "unset",
    override_btn: "Override",
    delete_override_confirm:
      "Remove this override? The tenant falls back to the platform credential.",
    edit_override_modal_title: "Edit tenant override for {{key}}",
    embedding_heading: "Embedding & Rerank",
    embedding_current: "Current",
    embedding_unconfigured:
      "No embedding model configured — long-term memory is unavailable until you set one.",
    embedding_provider_label: "Embedding provider",
    embedding_model_label: "Embedding model",
    rerank_enable: "Enable rerank",
    rerank_provider_label: "Rerank provider",
    rerank_model_label: "Rerank model",
    embedding_save: "Save",
    embedding_saved: "Embedding configuration saved.",
    embedding_err_EMBEDDING_PROVIDER_KEY_MISSING:
      "This provider has no configured key — add it under Providers above first.",
    embedding_err_INVALID_EMBEDDING_MODEL:
      "That model can't be used for embeddings.",
    embedding_err_INVALID_RERANK_PAIR:
      "Set both rerank provider and model, or neither.",
    embedding_err_RERANK_PROVIDER_KEY_MISSING:
      "The rerank provider has no configured key.",
    embedding_err_INVALID_RERANK_MODEL: "That model can't be used for rerank.",
    judge_heading: "Output Judge",
    judge_help_title: "What is the judge model?",
    judge_help_body:
      "A separate model that reviews agent replies (and, for tool-using agents, tool calls) for signs of prompt-injection hijacking or secret leakage — a safety backstop above the rule-based screen. Pick a cheap, fast model: it is called often. Leave it unset to reuse each agent's own primary model (more accurate, but pricier and slower). It only runs when an agent's manifest enables output_judge / action_screen.",
    judge_current: "Current judge model",
    judge_unconfigured: "Not set — judges reuse each agent's own model.",
    judge_provider_label: "Judge provider",
    judge_model_label: "Judge model",
    judge_save: "Save",
    judge_saved: "Judge configuration saved.",
    judge_clear: "Clear (use agent's own model)",
    judge_err_INVALID_JUDGE_PAIR:
      "Set both judge provider and model, or neither to clear.",
    judge_err_JUDGE_PROVIDER_KEY_MISSING:
      "This provider has no configured key — add it under Providers above first.",
    judge_err_INVALID_JUDGE_MODEL:
      "That model can't be used as a judge (pick a chat model).",
    billing_heading: "Cost rollup",
    billing_help_title: "What is cost rollup?",
    billing_help_body:
      "An offline job prices each tenant's token usage with the rate cards and writes the monthly cost ledger that powers the chargeback view. It runs on a schedule. Turn it off to pause cost computation platform-wide (the schedule keeps firing but each run skips); turn it back on to resume.",
    billing_title: "Cost rollup",
    billing_enabled: "Cost rollup enabled",
    billing_disabled: "Cost rollup paused",
    billing_hint:
      "While paused, the chargeback / usage cost figures stop updating; raw token metering is unaffected.",
    billing_saved: "Cost rollup setting saved.",
    billing_save_failed: "Failed to save cost rollup setting",
    tool_budget_heading: "Tool-output budget",
    tool_budget_help_title: "What is the tool-output budget?",
    tool_budget_help_body:
      "Large or accumulated tool results (e.g. web search) are externalized to the workspace and pruned from context so long runs stay within the model window. This is the platform master switch — turning it off reverts the whole feature (externalization + prune) for every agent.",
    tool_budget_title: "Tool-output budget",
    tool_budget_enabled: "Tool-output budget enabled",
    tool_budget_disabled: "Tool-output budget disabled",
    tool_budget_hint:
      "Off reverts platform-wide on the next agent build (no redeploy). Per-agent switches can only narrow this further (effective = platform AND agent).",
    tool_budget_env_default: "env default",
    tool_budget_toggle_label: "Platform tool-output budget",
    tool_budget_saved: "Tool-output budget setting saved.",
    tool_budget_save_failed: "Failed to save tool-output budget setting",
    dynamic_worker_heading: "Dynamic worker guardrails",
    dynamic_worker_help_title: "What are dynamic worker guardrails?",
    dynamic_worker_help_body:
      "Limits on the spawn_worker tool: how many temporary sub-agent workers a run may spawn at once, how many it may spawn cumulatively in one run, and how many steps each worker may take. Unset falls back to the process env-default settings.",
    dynamic_worker_max_concurrent_label: "Max concurrent workers per run",
    dynamic_worker_max_per_run_label: "Max cumulative workers per run",
    dynamic_worker_max_iterations_label: "Max steps per worker",
    dynamic_worker_env_default: "env default",
    dynamic_worker_hint:
      "Changes take effect on the next run/build — no restart needed. When unset, the env-default values are used.",
    dynamic_worker_save: "Save",
    dynamic_worker_saved: "Dynamic worker guardrails saved.",
    dynamic_worker_save_failed: "Failed to save dynamic worker guardrails",
    delegation_heading: "Delegation-gate capacity",
    delegation_help_title: "What is the delegation-gate capacity?",
    delegation_help_body:
      "Caps how many sub-agent delegations may be in flight at once across the whole platform (per process). Unset falls back to the built-in default. Once the cap is reached, a new delegation queues for up to 30 seconds waiting for a slot; if none frees up in time, that delegation soft-fails without interrupting the run.",
    delegation_max_concurrent_delegations_label: "Max concurrent delegations (platform-wide)",
    delegation_env_default: "env default",
    delegation_hint:
      "Changes take effect on the next run/build — no restart needed. When unset, the built-in default is used.",
    delegation_save: "Save",
    delegation_saved: "Delegation-gate capacity saved.",
    delegation_save_failed: "Failed to save delegation-gate capacity",
    quality_heading: "Quality Monitoring",
    quality_help_title: "What is quality monitoring?",
    quality_help_body:
      "Samples a fraction of finished runs, has an LLM judge score them 1-5, stores a per-agent quality series, and raises a drift alert when scores regress. Enabling it spends judge tokens per sampled run.",
    quality_default_note:
      "Not configured yet — the values below are the environment defaults and monitoring is off. Save to enable and take over from the defaults.",
    quality_deploy_note:
      "The workers always run; this toggle is the operational switch. The EXPERT_WORK_ENABLE_QUALITY_MONITOR deploy flag can still force the whole subsystem off.",
    quality_section_master: "Enable",
    quality_enabled_label: "Quality monitoring enabled",
    quality_enabled_hint: "Off by default — sampling + judging spends tokens.",
    quality_section_sampling: "Sampling",
    quality_sampling_rate_label: "Sampling rate (%)",
    quality_daily_cap_label: "Per-tenant daily cap",
    quality_monitor_interval_label: "Sampler interval (s)",
    quality_batch_size_label: "Sampler batch size",
    quality_section_judge: "Judge model",
    quality_judge_provider_label: "Judge provider",
    quality_judge_model_label: "Judge model",
    quality_judge_hint:
      "A provider with a configured platform key + a chat model. The judge scores replies; a cheap fast model is fine.",
    quality_section_drift: "Drift detection",
    quality_drift_interval_label: "Drift check interval (s)",
    quality_recent_window_label: "Recent window (h)",
    quality_baseline_window_label: "Baseline window (h)",
    quality_min_samples_label: "Min samples per window",
    quality_threshold_label: "Drift threshold (0-1)",
    quality_cooldown_label: "Alert cooldown (h)",
    quality_save: "Save",
    quality_saved: "Quality config saved",
    quality_save_error: "Failed to save quality config",
    quality_err_JUDGE_PROVIDER_KEY_MISSING:
      "Configure that provider's key in platform credentials before selecting it as the quality judge.",
    quality_err_INVALID_JUDGE_MODEL: "That is not a chat model for the selected provider.",
  },
  settings_iam: {
    sa_page_title: "Service Accounts",
    sa_subtitle:
      "Non-human identities — bots, workflows, integrations. Each service account holds zero or more API keys.",
    sa_api_keys_hint: "Manage the keys themselves on the API Keys page.",
    sa_create: "Create Service Account",
    sa_create_modal_title: "Create service account",
    sa_created: "Service account created.",
    sa_deleted: "Service account deleted.",
    sa_empty_home: "No service accounts in this tenant yet.",
    sa_empty_cross: "No service accounts across all tenants.",
    sa_delete_confirm_title: "Delete this service account?",
    sa_delete_confirm_body:
      "All API keys bound to this service account will stop authenticating immediately.",
    rb_page_title: "Role Bindings",
    rb_subtitle:
      "Bind a user or service account to a role. platform_scope=true grants cross-tenant SYSTEM_ADMIN; only system_admins can create those bindings.",
    rb_create: "Create Role Binding",
    rb_create_drawer_title: "Create role binding",
    rb_create_submit: "Create",
    rb_create_platform_submit: "Create platform-scope binding",
    rb_filter_platform_scope: "Show platform-scope only",
    rb_created: "Role binding created.",
    rb_deleted: "Role binding deleted.",
    rb_empty_home: "No role bindings in this tenant yet.",
    rb_empty_cross: "No role bindings across all tenants.",
    rb_empty_platform: "No platform-scope bindings.",
    rb_delete_confirm_title: "Delete this role binding?",
    rb_delete_confirm_body: "The subject loses this role immediately.",
    rb_delete_platform_warn:
      "Deleting a platform-scope binding revokes cross-tenant SYSTEM_ADMIN from the subject.",
    rb_confirm_required:
      "Type the confirmation phrase exactly to authorize the platform-scope binding.",
    cross_tenant_banner: "cross-tenant view",
    failed_to_load: "Failed to load",
    col_name: "Name",
    col_description: "Description",
    col_active: "Active",
    col_created: "Created",
    col_subject_type: "Subject type",
    col_subject_id: "Subject ID",
    col_role: "Role",
    col_tenant: "Tenant",
    col_conditions: "Conditions",
    col_granted_at: "Granted at",
    col_actions: "Actions",
    conditions_none: "type-wide",
    section_conditions: "ABAC conditions (optional)",
    conditions_hint:
      "Narrow this grant to matching resource instances. Leave empty for type-wide access.",
    field_resource_ids: "Resource IDs",
    resource_ids_hint: "Agent names this role may act on. Empty = any.",
    resource_ids_placeholder: "agent-foo, agent-bar",
    field_labels: "Labels",
    labels_hint:
      "Match resources whose metadata.labels superset these (k=v, comma-separated).",
    field_owner_only: "Owner only (resources created by this subject)",
    field_name: "Name",
    field_description: "Description",
    field_subject_type: "Subject type",
    field_subject_id: "Subject ID (UUID)",
    field_role: "Role",
    field_platform_scope: "platform_scope (cross-tenant)",
    platform_scope_hint:
      "Only system_admins can set this. Required when role=system_admin.",
    platform_scope_warn_title: "Cross-tenant elevation",
    platform_scope_warn_body:
      "This grants the subject SYSTEM_ADMIN across every tenant. Cannot be undone except by deleting the binding. Operation will be audited.",
    role_requires_platform_scope:
      "role=system_admin requires platform_scope=true (backend DTO validator).",
    confirm_phrase_label: "Type {{phrase}} to confirm",
    name_required: "Name is required",
    subject_id_required: "Subject ID is required",
    role_required: "Role is required",
  },
  settings_members: {
    page_title: "Members",
    subtitle:
      "People in this tenant and their invitation lifecycle. Invite by email, resend pending invites, or remove a member.",
    invite: "Invite",
    filter_all: "All",
    col_email: "Email",
    col_name: "Name",
    col_role: "Role",
    col_status: "Status",
    col_tenant: "Tenant",
    col_invited_at: "Invited at",
    col_actions: "Actions",
    cross_tenant_banner:
      "Cross-tenant view — read-only. Switch to a single tenant to invite or manage members.",
    status_invited: "Invited",
    status_active: "Active",
    status_suspended: "Suspended",
    status_revoked: "Revoked",
    resend: "Resend",
    remove: "Remove",
    set_password: "Set password",
    set_password_title: "Set a temporary password",
    set_password_hint: "The member must change it on first login.",
    set_password_label: "Temporary password",
    set_password_placeholder: "At least 8 characters",
    set_password_submit: "Set password",
    set_password_ok: "Password set.",
    set_password_failed: "Failed to set password",
    set_password_too_short: "At least 8 characters",
    revoke_confirm_title: "Revoke this invite?",
    revoke_confirm_body:
      "The pending invitation is cancelled and the link stops working immediately.",
    suspend_confirm_title: "Suspend this member?",
    suspend_confirm_body:
      "The member loses access immediately. They can be re-invited later.",
    invite_drawer_title: "Invite a member",
    field_email: "Email",
    field_role: "Role",
    field_display_name: "Display name",
    field_display_name_placeholder: "Optional",
    email_required: "Email is required",
    email_invalid: "Enter a valid email address",
    role_required: "Role is required",
    invite_submit: "Send invite",
    invite_success: "Invitation sent.",
    invite_partial_fail: "Some invitations failed: {{detail}}",
    resent: "Invitation resent.",
    removed: "Member removed.",
    failed_to_load: "Failed to load members",
    empty: "No members in this tenant yet.",
    empty_cross: "No members across all tenants.",
    purge_action: "Deactivate & purge",
    purge_confirm_title: "Deactivate and purge this member · irreversible",
    purge_confirm_body:
      "This deactivates {{email}}, deletes their Keycloak account, removes their role bindings, and irreversibly purges their business data. If some steps fail it is safe to run again.",
    purge_type_to_confirm: "Type the member's email to confirm:",
    purge_no_data_note:
      "This member has never signed in — there is no business data to purge; the account is deactivated and deleted only.",
    purge_done: "Member deactivated and purged.",
    purge_partial: "Some steps failed — safe to run again to finish cleanup.",
  },
  settings_platform_users: {
    page_title: "Platform Admins",
    subtitle:
      "Platform-level system_admin grants. Admins see every tenant and manage platform credentials, the MCP catalog and other cross-tenant settings. system_admin only.",
    not_admin_title: "system_admin only",
    not_admin_body:
      "Platform admins are managed by existing system_admins. Ask one to grant you the role.",
    failed_to_load: "Failed to load platform admins.",
    col_subject: "Subject",
    col_role: "Role",
    col_granted_by: "Granted by",
    col_granted_at: "Granted at",
    col_actions: "Actions",
    you: "You",
    grant: "Grant admin",
    granted: "Platform admin granted.",
    revoke: "Revoke",
    revoked: "Platform admin revoked.",
    revoke_confirm: "Revoke this platform admin?",
    revoke_self_warning:
      "This is your own grant — revoking it removes your platform access.",
    subject_label: "Subject UUID",
    subject_hint:
      "The Keycloak user UUID (the `sub` claim), not an email address.",
    subject_invalid: "Enter a valid UUID (Keycloak `sub`).",
  },
  triggers: {
    page_title: "Triggers",
    subtitle:
      "Agent auto-start hooks — cron expression timers or external webhook ingest (HMAC secret auth). Cross-tenant view requires system_admin.",
    cross_tenant_banner: "cross-tenant view",
    failed_to_load: "Failed to load triggers",
    empty_cron: "No cron triggers yet.",
    empty_webhook: "No webhook triggers yet.",
    empty_cross: "No triggers across all tenants.",
    tab_cron: "Cron",
    tab_webhook: "Webhook",
    col_name: "Name",
    col_agent: "Agent",
    col_cron_expr: "Cron expression",
    col_webhook_path: "Webhook path",
    col_enabled: "Enabled",
    col_updated: "Updated",
    col_actions: "Actions",
    create: "Create Trigger",
    create_submit: "Create",
    create_cron_title: "Create cron trigger",
    create_webhook_title: "Create webhook trigger",
    field_name: "Name",
    field_agent_name: "Agent name",
    field_agent_version: "Agent version",
    field_cron_expr: "Cron expression",
    name_required: "Name is required",
    agent_required: "Agent name is required",
    agent_version_required: "Agent version is required",
    cron_required: "Cron expression is required",
    cron_hint: 'Standard 5-field cron, e.g. "0 9 * * *" runs at 09:00 daily.',
    webhook_secret_info_title: "Webhook secret will be generated server-side",
    webhook_secret_info_body:
      "After creation a one-time secret will be shown. Save it now — there is no way to retrieve it later (rotation = delete + re-create in M0).",
    secret_drawer_title: "Webhook secret (show once)",
    secret_warn_title: "Save this secret now",
    secret_warn_body:
      "This is the only time the full secret is shown. Treat it like a password — copy it to your secret manager before closing.",
    secret_trigger_name: "Trigger",
    secret_label: "Secret",
    secret_copy: "Copy",
    secret_copied: "Secret copied to clipboard.",
    secret_copy_failed: "Clipboard copy failed — manually copy from the field.",
    secret_acknowledged: "I saved the secret",
    toggled: "Trigger updated.",
    created: "Trigger created.",
    deleted: "Trigger deleted.",
    delete_confirm_title: "Delete this trigger?",
    delete_confirm_body:
      "The cron schedule / webhook endpoint will stop firing immediately. Cannot be undone.",
  },
  webhooks: {
    page_title: "Webhooks",
    subtitle:
      "Outbound webhook endpoints — the platform signs and POSTs agent-lifecycle events (run completed / failed, approval requested, artifact saved) to your URL.",
    cross_tenant_banner: "Cross-tenant view",
    failed_to_load: "Failed to load webhook endpoints",
    empty:
      "No webhook endpoints yet. Create one to receive signed event deliveries.",
    empty_cross: "No webhook endpoints across any tenant.",
    all_agents: "All agents",
    col_name: "Name",
    col_url: "URL",
    col_events: "Events",
    col_agent: "Agent",
    col_enabled: "Enabled",
    col_actions: "Actions",
    create: "New endpoint",
    create_submit: "Create",
    create_title: "New webhook endpoint",
    field_name: "Name",
    field_url: "Delivery URL",
    url_hint:
      "HTTPS endpoint the platform POSTs to. Private / metadata addresses are rejected.",
    field_events: "Event types",
    events_placeholder: "Select one or more events",
    field_agent_name: "Agent scope (optional)",
    agent_name_hint:
      "Leave blank to receive events from every agent in the tenant.",
    field_payload_format: "Delivery format",
    payload_format_hint:
      "Generic = the signed Expert Work event JSON; IM formats render the event as a text bot message — use the platform's incoming-webhook robot URL.",
    col_format: "Format",
    format_generic: "Generic",
    format_feishu: "Feishu bot",
    format_dingtalk: "DingTalk bot",
    format_wecom: "WeCom bot",
    name_required: "Name is required",
    url_required: "URL is required",
    events_required: "Select at least one event type",
    secret_drawer_title: "Signing secret — shown once",
    secret_warn_title: "Copy this secret now",
    secret_warn_body:
      "This HMAC signing secret is shown only once and never again. Verify the X-Expert-Work-Signature-256 header with it. Rotation = delete + re-create.",
    secret_endpoint_name: "Endpoint",
    secret_label: "Signing secret",
    secret_copy: "Copy",
    secret_copied: "Secret copied to clipboard",
    secret_copy_failed: "Copy failed — select the text manually",
    secret_acknowledged: "I've saved it",
    toggled: "Endpoint updated",
    created: "Webhook endpoint created",
    deleted: "Webhook endpoint deleted",
    delete_confirm_title: "Delete this webhook endpoint?",
    delete_confirm_body:
      "Deliveries to this endpoint stop immediately. Cannot be undone.",
  },
  skills: {
    page_title: "Skills",
    subtitle:
      "Reusable skill library — each skill carries versions with prompt fragments + tool name allow-lists. Import / export as .skill ZIP for cross-instance sync.",
    cross_tenant_banner: "cross-tenant view",
    failed_to_load: "Failed to load skills",
    empty_home: "No skills in this tenant yet.",
    empty_cross: "No skills across all tenants yet.",
    col_name: "Name",
    revision_pending: "Revision pending review",
    revision_pending_tooltip:
      "This draft is a new revision (v{{version}}) of an existing evolved skill — dedup folded a similar distillate into it instead of creating a new entry.",
    col_status: "Status",
    col_category: "Category",
    col_description: "Description",
    col_updated: "Updated",
    col_created: "Created",
    col_version: "Version",
    col_tools: "Tools",
    col_authored_by: "Authored by",
    col_actions: "Actions",
    filter_status: "Status filter",
    filter_status_all: "All statuses",
    filter_category: "Category",
    load_more: "Load more",
    create: "Create",
    create_modal_title: "Create skill (empty draft)",
    import_zip: "Import ZIP",
    export_zip: "Export ZIP",
    field_name: "Skill name (a-z, 0-9, _, -)",
    field_category: "Category",
    field_description: "Description",
    name_required: "Name is required",
    category_required: "Category is required",
    description_required: "Description is required",
    create_hint:
      "Empty draft — add a version (prompt fragment + tool names) via API or ZIP import to make it usable.",
    created: "Skill created.",
    imported: "Imported {{name}} v{{version}}.",
    status_changed: "Status changed to {{status}}.",
    status_draft: "Draft",
    status_active: "Active",
    status_stale: "Stale",
    status_archived: "Archived",
    change_status: "Change status",
    metadata_title: "Metadata",
    versions_title: "Versions",
    no_versions: "No versions yet — add one via API or ZIP import.",
    latest_version_hint: "Latest version number",
    detail_files_title: "Files",
    detail_editor_title: "Editor",
    detail_no_file_selected: "Select a file from the tree to view or edit it.",
    detail_select_file_hint:
      "All edits create a new immutable version — the prior version stays intact for rollback.",
    detail_skill_md_pinned: "SKILL.md (main body)",
    detail_supporting_files_section: "Supporting files",
    detail_no_supporting_files:
      "No supporting files. Use + Add to upload reference docs, prompts, or scripts.",
    detail_version_picker_label: "Version",
    detail_active_version_marker: "(latest)",
    detail_lazy_badge: "Lazy",
    detail_lazy_tooltip:
      "Body fetched on-demand via the skill_view tool — only the summary is in the system prompt.",
    detail_eager_tooltip:
      "Body eager-loaded into the system prompt at agent build time.",
    detail_high_risk_badge: "High-risk",
    detail_high_risk_tooltip:
      "Declares exec_python / exec_shell / http, or carries scripts/* files — Activate requires admin role.",
    detail_high_risk_warning:
      "This version is high-risk. Review the supporting files + tool list carefully before activating.",
    detail_admin_required_tooltip: "Contact a tenant admin to activate.",
    detail_unsaved_changes_warning:
      "You have unsaved changes. Save or cancel before switching files.",
    detail_diff_toggle: "Show diff vs. server",
    detail_diff_original_label: "Server",
    detail_diff_draft_label: "Draft",
    file_add: "+ Add file",
    file_add_modal_title: "Add supporting file",
    file_add_path_label: "Relative path",
    file_add_path_placeholder: "e.g. reference/error_codes.md",
    file_add_content_label: "Content (text)",
    file_add_upload_label: "Or upload a file",
    file_add_upload_hint:
      "Max 1 MB per file, 5 MB per skill total. Path / extension validated server-side.",
    file_add_submit: "Add",
    file_action_edit: "Edit",
    file_action_delete: "Delete",
    file_action_rename: "Rename",
    file_action_save: "Save",
    file_action_cancel: "Cancel",
    file_save_failed: "Save failed",
    file_saved: "Saved as v{{version}}.",
    file_deleted: "Deleted — new v{{version}} created.",
    file_delete_confirm_title: "Delete {{path}}?",
    file_delete_confirm_body:
      "A new SkillVersion will be created without this file. The prior version stays intact.",
    file_delete_confirm_input_hint: "Type the file path to confirm",
    file_rename_modal_title: "Rename {{path}}",
    file_rename_new_path_label: "New relative path",
    file_rename_submit: "Rename",
    file_renamed: "Renamed — new v{{version}} created.",
    file_binary_placeholder:
      "[BINARY: {{size}} bytes, mime={{mime}}] — preview disabled. Export the ZIP to inspect.",
    file_size_label: "Size",
    file_mime_label: "MIME",
    file_load_failed: "Failed to load file",
    detail_skill_md_edit_hint:
      "SKILL.md is the skill's core prompt. Saving an edit creates a new version and keeps all supporting files.",
    pin: "Lock",
    unpin: "Unlock",
    pinned_toast: "Locked — auto-cleanup won't mark this skill stale/archived.",
    unpinned_toast:
      "Unlocked — auto lifecycle resumes (long-unused skills age out).",
    pin_tooltip_on:
      "Locked — auto-cleanup never marks this skill stale/archived, no matter how long it goes unused.",
    pin_tooltip_off:
      "Lock: stop auto-cleanup from ever marking this skill stale/archived (by default a long-unused skill goes stale, then archived).",
    eta_days_to_stale: "{{days}}d to stale",
    eta_due_soon: "stale soon",
    source_platform: "Platform",
    source_tenant: "Mine",
    requires_tier: "Requires {{tier}}",
    requires_tier_tooltip:
      "Your plan doesn't include this skill — upgrade to {{tier}} to use it.",
  },
  agent_template_marketplace: {
    page_title: "Template Marketplace",
    subtitle:
      "Browse the platform-curated Agent templates and fork one into your own agent.",
    failed_to_load: "Failed to load the template marketplace",
    empty: "No platform templates are available yet.",
    tier_free: "Free",
    tier_pro: "Pro",
    tier_enterprise: "Enterprise",
    requires_tier: "Requires {{tier}}",
    locked_ribbon: "Locked",
    fork: "Fork",
    fork_title: "Fork “{{name}}”",
    fork_name_label: "New agent name",
    fork_name_hint:
      "Used as the agent_code external apps call. Lowercase letters, digits, and hyphens.",
    fork_name_required: "An agent name is required",
    fork_name_invalid:
      "Use lowercase letters, digits, and hyphens (must start with a letter or digit)",
    forked_ok: "Forked into “{{name}}”",
  },
  agent_templates: {
    page_title: "Agent Templates",
    subtitle: "Platform-curated base agents that tenants fork into their own.",
    add: "New template",
    deleted: "Template deleted",
    delete_confirm: "Delete “{{name}}”?",
    not_admin_title: "System admin only",
    not_admin_body: "Only a platform system admin can manage Agent templates.",
    failed_to_load: "Failed to load templates",
    empty_title: "No templates yet",
    empty_hint: "Create a base Agent template tenants can fork.",
    col_name: "Template",
    col_category: "Category",
    col_tier: "Tier",
    col_status: "Status",
    col_enabled: "Enabled",
    col_actions: "Actions",
    tier_free: "Free",
    tier_pro: "Pro",
    tier_enterprise: "Enterprise",
    status_draft: "Draft",
    status_published: "Published",
    create_title: "New Agent template",
    create_btn: "Create",
    invalid_manifest: "The manifest is not valid YAML / JSON.",
    save_failed: "Save failed",
    saved_ok: "Saved",
    yes: "Yes",
    no: "No",
    back: "Back",
    field_display_name: "Display name",
    display_name_required: "Display name is required",
    field_description: "Description",
    field_category: "Category",
    field_icon: "Icon (emoji)",
    field_tier: "Required tier",
    field_status: "Status",
    field_enabled: "Enabled",
    tab_basic: "Basic info",
    field_display_name_help:
      "The template's name shown to tenants in the marketplace.\nExample: Smart Support Bot",
    field_description_help:
      "Short description of the template's purpose, helping tenants decide whether to fork.\nExample: Ready-to-use multi-turn support agent",
    field_category_help:
      "Category for marketplace filtering.\nExample: Support / Sales / Coding",
    field_icon_help: "An emoji shown on the marketplace card.\nExample: 🤖",
    field_tier_help:
      "The tenant plan tier required to fork this template. Lower-tier tenants see it but can't fork.\nExample: Free / Pro / Enterprise",
    field_status_help:
      "Draft is admin-only; Published makes it visible + forkable in the marketplace.\nExample: Draft while testing, Publish when ready",
    field_enabled_help:
      "Master switch. Off removes it from the marketplace even if published (without deleting).\nExample: turn off to temporarily delist",
    cat_support: "Support",
    cat_sales: "Sales",
    cat_research: "Research",
    cat_coding: "Coding",
    cat_data: "Data",
    cat_productivity: "Productivity",
    cat_general: "General",
    cat_other: "Other",
  },
  platform_skills: {
    page_title: "Platform Skills",
    subtitle:
      "Curate reusable skills tenants can bind to their agents. Platform-level, system admins only.",
    add: "New skill",
    import_zip: "Import .skill",
    import_github: "Import from GitHub",
    github_modal_title: "Import skill from GitHub",
    github_hint:
      "Pull a skill from a public GitHub repo. Accepts owner/repo, a github.com URL, or a skills.sh URL. For a repo with many skills, name which one.",
    github_source_label: "Source",
    github_source_ph: "vercel-labs/skills  ·  github.com/…  ·  skills.sh/…",
    github_skill_label: "Skill",
    github_skill_ph: "find-skills (optional — required if the repo has many)",
    github_ref_label: "Ref",
    github_ref_ph: "branch / tag / SHA (optional, default branch)",
    github_source_help:
      "The GitHub repo to import skills from — owner/repo or full URL.\nExample: anthropics/skills",
    github_skill_help:
      "Which skill folder(s) inside the repo to import. Leave a single name, or pick from the detected list.\nExample: pptx",
    github_ref_help:
      "Git ref to import from — branch, tag, or commit SHA. Blank = the repo's default branch.\nExample: main",
    github_submit: "Import",
    github_pick_skill:
      "This repo has {{count}} skills — pick one or more to import.",
    github_pick_ph: "Select skills",
    github_select_all: "Select all",
    github_clear_all: "Clear all",
    github_batch_submit: "Import {{count}} selected",
    github_batch_done: "Imported {{ok}}, {{failed}} failed.",
    github_result_created: "Created",
    github_result_exists: "Exists",
    github_result_failed: "Failed",
    imported: 'Imported "{{name}}" v{{version}}.',
    import_noop: 'No change to "{{name}}" — already the latest version.',
    manage: "Manage",
    pin: "Lock",
    unpin: "Unlock",
    batch_toolbar_aria: "Bulk-edit the selected skills",
    batch_select_row: "Select skill {{name}}",
    batch_select_all: "Select all skills on this page",
    batch_selected: "{{count}} selected",
    batch_lock: "Lock",
    batch_unlock: "Unlock",
    batch_archive: "Archive",
    batch_activate: "Activate",
    batch_clear: "Clear",
    batch_done: "Done: {{ok}} updated",
    batch_partial: "Done: {{ok}} updated, {{failed}} failed",
    batch_all_matching: "Apply to all {{count}} matching",
    batch_scope_all: "All {{count}} matching",
    batch_set_category: "Set category",
    batch_clear_category: "Clear category",
    search_ph: "Search name or description",
    search_aria: "Search platform skills",
    status_filter_aria: "Filter by status",
    status_filter_all: "All statuses",
    not_admin_title: "System admin only",
    not_admin_body:
      "The platform skill catalog is managed by system admins. Ask one to add skills.",
    failed_to_load: "Failed to load platform skills",
    col_name: "Name",
    col_category: "Category",
    col_description: "Description",
    col_tier: "Required plan",
    col_status: "Status",
    col_version: "Version",
    col_actions: "Actions",
    no_description: "No description",
    category_filter_all: "All categories",
    category_filter_aria: "Filter by category",
    category_uncategorized: "Uncategorized",
    empty_title: "No platform skills yet",
    empty_hint:
      "Add curated skills so every tenant can bind battle-tested capabilities without reinventing them.",
    tier_free: "Free",
    tier_pro: "Pro",
    tier_enterprise: "Enterprise",
    status_draft: "Draft",
    status_active: "Active",
    status_archived: "Archived",
    detail_category_label: "Category",
    detail_category_placeholder: "e.g. mental-health, data, writing",
    detail_category_save: "Save category",
    detail_category_saved: "Category updated",
    detail_category_help:
      "The organizing label shown in the skill list. Pick an existing category or type a new one; leave empty to clear.",
    create_title: "New platform skill",
    create_submit: "Create",
    field_name: "Name (slug)",
    field_name_hint: "Lowercase identifier, immutable after creation",
    field_category: "Category",
    field_description: "Description",
    field_required_tier: "Required plan",
    name_required: "A valid lowercase slug is required",
    created: "Platform skill created.",
    duplicate_name: "A platform skill with this name already exists.",
    when_to_use_hint:
      "Describe WHEN to use this skill — model-driven selection keys off this text.",
    manage_title: "Manage {{name}}",
    lifecycle_title: "Lifecycle",
    add_version_title: "Add version",
    add_version_submit: "Add version",
    field_prompt_fragment: "Prompt fragment",
    field_version_description: "Description",
    field_tool_names: "Tool names",
    field_required_models: "Required models",
    prompt_fragment_required: "Prompt fragment is required",
    csv_hint: "Comma-separated.",
    versions_title: "Versions",
    no_versions: "No versions yet — add one above to make this skill usable.",
    version_added: "Added v{{version}}.",
    status_changed: "Status changed to {{status}}.",
    pinned: "Locked",
    unpinned: "Unlocked",
    high_risk: "High-risk",
    lazy: "Lazy",
  },
  memory: {
    page_title: "Memory",
    subtitle:
      "Per-user long-term memories used for recall during agent runs. Edit / delete here is destructive; vector embeddings re-compute on PATCH.",
    cross_tenant_banner: "cross-tenant view",
    failed_to_load: "Failed to load memories",
    empty_home: "No memories in this tenant yet.",
    empty_cross: "No memories across all tenants yet.",
    col_kind: "Kind",
    col_content: "Content",
    col_score: "Scores",
    col_user: "User",
    col_created: "Created",
    col_actions: "Actions",
    importance_abbr: "Imp",
    confidence_abbr: "Conf",
    importance_tip:
      "Importance (0–1) — how reusable this memory is; the write-filter drops low scores.",
    confidence_tip:
      "Confidence (0–1) — extraction certainty; 1.0 after a user correction.",
    filter_kind: "Kind filter",
    filter_kind_all: "All kinds",
    search_placeholder: "Filter by content (client-side)",
    filter_user_placeholder: "Filter by user id (UUID)",
    edit_title: "Edit memory",
    correct: "Correct",
    correct_title: "Correct memory",
    corrected: "Memory corrected.",
    edit_meta_user: "User",
    edit_meta_kind: "Kind",
    edit_content_label: "Content",
    save_dirty: "Save (edited)",
    embedder_note:
      "PATCH re-computes the embedding so vector recall stays consistent with the new content.",
    embedder_unconfigured:
      "Backend has no embedder configured — memory PATCH refused (EXPERT_WORK_EMBEDDING_API_KEY_REF + MODEL required).",
    empty_content: "Content cannot be empty.",
    updated: "Memory updated.",
    deleted: "Memory deleted.",
    delete_confirm_title: "Delete this memory?",
    delete_confirm_body:
      "Soft-delete; row recoverable for 30 days then gone permanently.",
  },
  eval_datasets: {
    failed_to_load: "Failed to load eval datasets",
    empty_home: "No eval datasets in this tenant yet.",
    empty_cross: "No eval datasets across all tenants yet.",
    col_name: "Name",
    col_agent: "Agent",
    col_source: "Source",
    col_updated: "Updated",
    col_actions: "Actions",
    create: "Create",
    create_modal_title: "Create eval dataset",
    field_agent_name: "Agent name",
    field_name: "Dataset name",
    agent_required: "Agent name is required",
    name_required: "Name is required",
    edit_title: "Edit eval dataset",
    edit_input_label: "Input (JSON object)",
    edit_expected_label: "Expected output (JSON object or empty for null)",
    json_parse_error: "JSON parse error",
    created: "Eval dataset created.",
    updated: "Eval dataset updated.",
    deleted: "Eval dataset deleted.",
    delete_confirm_title: "Delete this eval dataset?",
    delete_confirm_body:
      "This row will be removed from the golden suite. Cannot be undone.",
  },
  api_keys: {
    page_title: "API Keys",
    subtitle:
      "Service-account access keys. Each key carries scopes (read / write / admin). Supports rotation (double-active grace) and immediate revocation.",
    create: "Create API Key",
    failed_to_load: "Failed to load API keys",
    empty: "No API keys yet. Create one bound to a service account to start.",
    never: "never",
    rotation_banner: "{{count}} keys in rotation grace window",
    rotation_help:
      "Old keys are still valid until the grace window expires; have callers swap to the new key now.",
    col_prefix: "Prefix",
    col_scopes: "Scopes",
    col_service_account: "Service Account",
    col_status: "Status",
    col_last_used: "Last used",
    col_expires: "Expires",
    rotate: "Rotate",
    revoke: "Revoke",
    revoke_confirm: "Revoke this key immediately?",
    rotated:
      "API key rotated. Old key remains valid until the grace window expires.",
    revoked: "API key revoked.",
    service_account_label: "Service Account",
    service_account_required: "Pick a service account",
    scopes_label: "Scopes",
    scopes_required: "Pick at least one scope",
    expires_label: "Expires at",
    dangerous: "dangerous",
    show_once_title: "API Key created",
    show_once_ack: "I saved the key, close",
    show_once_help_prefix: "Copy this key immediately and store it securely — ",
    show_once_help_emphasis: "you will not be able to view the full key again",
    show_once_prefix_note: "Only the prefix is retained on the list:",
    copy: "Copy",
    copied: "Copied to clipboard",
  },
  tenant: {
    home_label_prefix: "Home",
    home_tenant: "Home tenant",
    all_tenants: "All tenants",
    your_tenant: "your tenant",
    system_admin_hint: "system admin",
    cross_tag: "cross",
  },
  agents_page: {
    page_title: "Agents",
    cross_tenant_banner: "cross-tenant view",
    failed_to_load: "Failed to load agents",
    empty_home: "No agents in this tenant yet — click Create to add one.",
    empty_cross: "No agents across all tenants yet.",
    column_name: "Name",
    column_status: "Status",
    column_owner: "Owner",
    column_tenant: "Tenant",
    column_created: "Created",
    column_actions: "Actions",
    status_active: "active",
    status_draft: "draft",
    status_archived: "archived",
    status_deleted: "deleted",
    search_placeholder: "Search by name",
    filter_status: "Filter by status",
    filter_status_default: "Default (hides deleted)",
    filter_status_all: "All statuses",
    action_playground: "Playground",
    action_edit: "Edit manifest",
    action_runs: "Runs",
    action_delete: "Delete",
    action_disable: "Disable",
    action_enable: "Enable",
    delete_title: "Delete agent",
    delete_warning: "This deletes {{name}} v{{version}} — it cannot be undone from the UI.",
    delete_warning_desc:
      "Marks it as deleted (soft delete); other versions of this agent are not affected.",
    delete_type_to_confirm: "Type the agent name to confirm deletion:",
    delete_success: "Deleted {{name}} v{{version}}",
    delete_failed: "Failed to delete agent: {{error}}",
    disable_confirm_title: "Disable this agent?",
    disable_confirm_body:
      "Rejects new runs and sessions for {{name}} (all versions) and cancels its in-flight runs. You can re-enable it anytime.",
    disable_success: "Disabled {{name}} — cancelled {{count}} in-flight run(s)",
    disable_failed: "Failed to disable agent: {{error}}",
    enable_success: "Enabled {{name}}",
    enable_failed: "Failed to enable agent: {{error}}",
    create: "Create",
    from_template: "From template",
  },
  create_agent: {
    title: "Create Agent",
    hint: "Edit the manifest then click Create. The backend validates the YAML through ManifestLoader; errors surface here.",
    submit: "Create",
    cancel: "Cancel",
    create_failed: "Failed to create agent",
    embedding_required_title: "Configure platform embedding first",
    embedding_required_desc:
      "New agents use long-term memory, which needs a platform embedding model. No embedding is configured yet — set one in Platform Settings, then create your agent.",
    embedding_required_cta: "Go to Platform Settings",
  },
  approvals_page: {
    page_title: "Approvals",
    cross_tenant_banner: "cross-tenant view (read-only)",
    filter_status: "Filter by status",
    failed_to_load: "Failed to load approvals",
    empty: "No approvals in this view — nothing is waiting on you.",
    column_reason: "Reason",
    column_action: "Requested action",
    column_waiting: "Waiting",
    column_timeout: "Time left",
    column_status: "Status",
    column_actions: "Decision",
    approve: "Approve",
    reject: "Reject",
    confirm_approve: "Approve this action?",
    confirm_reject: "Reject this action?",
    batch_approve: "Approve selected",
    batch_reject: "Reject selected",
    confirm_batch_approve: "Approve {{count}} selected actions?",
    confirm_batch_reject: "Reject {{count}} selected actions?",
    selected_count: "{{count}} selected",
    decide_ok: "{{count}} decision(s) applied",
    decide_partial:
      "{{ok}} applied, {{failed}} failed (already decided or gone) — list refreshed",
    decide_failed: "Decision request failed: {{error}}",
    select_row: "Select approval: {{summary}}",
    select_all: "Select all approvals on this page",
    waiting_minutes: "{{count}} min",
    waiting_hours: "{{count}} h",
    time_left_hours: "{{count}}h left",
    time_left_minutes: "{{count}}m left",
    timed_out: "timing out",
    approve_all_page: "Approve all pending ({{count}})",
    confirm_approve_all: "Approve all {{count}} pending actions on this page?",
    modify_hint:
      "To approve with edited arguments, open the run and use the approval card there.",
  },
  runs_page: {
    column_run_id: "Run ID",
    column_status: "Status",
    column_duration: "Duration",
    column_tokens: "Tokens",
    tokens_tip: "{{input}} in / {{output}} out · {{calls}} LLM call(s)",
    duration_running: "running",
    summary_title: "This run",
    summary_duration: "Duration",
    summary_tokens: "Tokens",
    summary_input: "in",
    summary_output: "out",
    summary_cache: "cache",
    summary_llm_calls: "LLM calls",
    summary_models: "Models",
    summary_resume: "resumed",
    summary_no_tokens: "No token usage recorded for this run.",
  },
  eval_runs_page: {
    page_title: "Eval Runs",
    subtitle: "Trigger and monitor capability evaluation runs.",
    failed_to_load: "Failed to load eval runs",
    empty: "No eval runs yet. Enqueue the baseline suite to start.",
    empty_cross: "No tenant has any eval runs yet.",
    col_tenant: "Tenant",
    column_run_id: "Run ID",
    column_status: "Status",
    column_suite: "Suite",
    column_summary: "Summary",
    column_created: "Created",
    filter_status: "Status filter",
    filter_status_all: "All statuses",
    suite_label: "Suite to run",
    enqueue: "Enqueue run",
    enqueue_success: "Queued an eval run.",
  },
  eval_run_detail: {
    failed_to_load: "Failed to load eval run",
    not_found: "eval run not found",
    run_metadata: "Run metadata",
    run_id: "Run ID",
    suite: "Suite",
    status: "Status",
    triggered_by: "Triggered by",
    created_at: "Created",
    started_at: "Started",
    finished_at: "Finished",
    summary: "Summary",
    cases_title: "Case results",
    cases_empty: "No case results recorded yet.",
    col_case_id: "Case",
    col_capability: "Capability",
    col_result: "Result",
    col_scores: "Scores",
    result_passed: "passed",
    result_failed: "failed",
  },
  usage: {
    page_title: "Usage",
    subtitle: "Billed cost and token usage for your tenant this month.",
    subtitle_cross:
      "Billed cost and token usage across every tenant on the platform this month (bucketed per tenant).",
    group_by_agent: "By Agent",
    group_by_model: "By Model",
    col_tenant: "Tenant",
    total_billed: "Total billed",
    as_of_note: "Cost as of {{time}}; updates hourly.",
    col_key: "Name",
    col_input_tokens: "Input tokens",
    col_output_tokens: "Output tokens",
    col_cache_creation_tokens: "Cache write tokens",
    col_cache_read_tokens: "Cache read tokens",
    col_hit_rate: "Cache hit rate",
    col_billed: "Billed cost",
    unpriced: "Unpriced",
    tokens_heading: "Token usage",
    kind_heading: "By purpose",
    col_kind: "Purpose",
    kind_conversation: "Conversation",
    kind_skill_evolution: "Skill evolution",
    kind_memory_consolidation: "Memory consolidation",
    realtime: "Realtime",
    realtime_note:
      "Live current-month counters; billed cost above lags by up to an hour.",
    empty: "No usage recorded for this month.",
    failed_to_load: "Failed to load usage",
  },
  chargeback: {
    page_title: "Chargeback",
    subtitle:
      "Cross-tenant cost split — base, markup, billed and margin per tenant.",
    not_admin_title: "system_admin only",
    not_admin_body:
      "The chargeback report is restricted to platform administrators.",
    tenant_filter: "Filter by tenant ID",
    as_of: "As of",
    total_base: "Total base cost",
    total_billed: "Total billed",
    total_margin: "Total margin",
    col_tenant: "Tenant",
    col_agent: "Agent",
    col_input_tokens: "Input tokens",
    col_output_tokens: "Output tokens",
    col_cache_read_tokens: "Cache read tokens",
    col_cache_creation_tokens: "Cache write tokens",
    col_hit_rate: "Cache hit rate",
    col_base: "Base cost",
    col_markup: "Markup",
    col_billed: "Billed",
    col_margin: "Margin",
    col_unpriced: "Unpriced buckets",
    empty: "No chargeback data for this month.",
    agents_empty: "No per-agent cost for this tenant this month.",
    failed_to_load: "Failed to load chargeback",
  },
  coming_soon: {
    title_prefix: "This page is being implemented",
    body: "Only the 4 core pages are available for now: Agents list / Agent detail (with Playground) / Run+Approval / Settings API Keys.",
    other_pages_prefix: "Other pages",
    other_pages_suffix: "will land in Stream H.",
  },
  cmdk: {
    placeholder: "Search or jump — type a command, Agent name, Run ID…",
    aria_label: "Command palette search",
    no_matches: "No matches",
    group_agents: "Agents",
    group_jump: "Jump",
    group_action: "Actions",
    label_conversations: "Conversations (across agents)",
    label_approvals: "Approvals (queue)",
    label_curation: "Curation review",
    label_eval: "Eval runs",
    label_memory: "Memory governance",
    label_knowledge: "Knowledge",
    label_skills: "Skills",
    label_agent_template_marketplace: "Template Marketplace",
    label_triggers: "Triggers",
    label_webhooks: "Webhooks",
    label_settings_api_keys: "Settings · API Keys",
    label_settings_platform_users: "Settings · Platform Admins",
    action_create_agent: "Create new Agent…",
    action_create_api_key: "Create new API Key…",
    action_open_settings: "Open Settings",
    hint_select: "Select",
    hint_jump: "Jump",
    hint_close: "Close",
    hint_shortcuts: "Type ? for shortcuts",
  },
  skill_evolution: {
    governance_title: "Evolution governance",
    visibility_agent_private: "Agent-private",
    visibility_tenant: "Tenant-wide",
    owner: "Owner",
    forked_from: "Forked",
    propose_to_tenant: "Propose to tenant",
    pending_tenant_promotion: "Pending tenant promotion",
    approve: "Approve",
    reject: "Reject",
    proposed_toast: "Promotion requested",
    approved_toast: "Promoted to tenant",
    rejected_toast: "Promotion rejected",
    no_version_to_propose: "This skill has no published version to propose",
    filter_visibility: "Visibility filter",
    filter_visibility_all: "All visibility",
    eval_title: "Replay evidence",
    eval_empty:
      "No replay verification yet — with too few tenant trajectories (a cold-start agent) distillation skips replay, so the skill awaits review without evidence rows.",
    eval_n_cases: "{{n}} cases",
    eval_aria: "baseline {{baseline}} vs with-skill {{skill}}",
    eval_baseline: "baseline",
    eval_with_skill: "with skill",
    verdict_pass: "pass",
    verdict_fail: "fail",
    verdict_inconclusive: "inconclusive",
    lineage_title: "Lineage",
    lineage_versions: "Versions",
    lineage_fork_aria: "fork lineage diagram",
    origin_human: "human",
    origin_in_session: "self-authored",
    origin_distilled: "distilled",
    kill_switch_engaged_toast: "Emergency stop engaged",
    kill_switch_released_toast: "Emergency stop released",
    kill_switch_confirm_title: "Engage emergency stop?",
    kill_switch_confirm_body:
      "Auto-promotion of new skills is degraded to human review until released.",
    kill_switch_engage: "Engage",
    kill_switch_hint: "Persistent emergency stop for auto-promotion",
    kill_switch_halted: "Auto-evolution halted",
    kill_switch_active: "Auto-evolution active",
    kill_switch_tenant_label: "Tenant",
    kill_switch_global_label: "Global",
    kill_switch_tenant_aria: "Toggle tenant auto-evolution emergency stop",
    kill_switch_global_aria: "Toggle global auto-evolution emergency stop",
  },
};

export default en;
