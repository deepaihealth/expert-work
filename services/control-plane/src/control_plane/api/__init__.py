"""FastAPI routers for the Control Plane (Streams B + C)."""

from control_plane.api.agent_schema import build_agent_schema_router
from control_plane.api.agent_templates import build_agent_templates_router
from control_plane.api.agent_users import build_agent_users_router, build_tenant_users_router
from control_plane.api.agents import build_agents_router
from control_plane.api.api_keys import build_api_keys_router
from control_plane.api.approvals import build_approvals_router
from control_plane.api.artifacts import build_artifacts_router
from control_plane.api.audit import build_audit_router
from control_plane.api.billing_admin import build_billing_admin_router
from control_plane.api.conversations import build_conversations_router
from control_plane.api.curation import build_curation_router, build_eval_dataset_router
from control_plane.api.eval_runs import build_eval_runs_router
from control_plane.api.external_approvals import build_external_approvals_router
from control_plane.api.external_events import build_external_events_router
from control_plane.api.external_runs import build_external_runs_router
from control_plane.api.external_sessions import build_external_sessions_router
from control_plane.api.external_uploads import build_external_uploads_router
from control_plane.api.external_workspace import build_external_workspace_router
from control_plane.api.feedback import build_feedback_router
from control_plane.api.health import build_health_router
from control_plane.api.knowledge import build_knowledge_router
from control_plane.api.mcp_catalog import build_mcp_catalog_router
from control_plane.api.mcp_oauth_api import build_mcp_oauth_router
from control_plane.api.mcp_servers import build_mcp_servers_router
from control_plane.api.me import build_me_router
from control_plane.api.members import build_members_router
from control_plane.api.memory import build_memory_router
from control_plane.api.metrics import build_metrics_router
from control_plane.api.model_catalog import build_model_catalog_router
from control_plane.api.plan import build_plan_router
from control_plane.api.platform_billing_config import (
    build_platform_billing_config_router,
)
from control_plane.api.platform_config import build_platform_config_router
from control_plane.api.platform_delegation_config import (
    build_platform_delegation_config_router,
)
from control_plane.api.platform_dynamic_worker_config import (
    build_platform_dynamic_worker_config_router,
)
from control_plane.api.platform_embedding_config import (
    build_platform_embedding_config_router,
)
from control_plane.api.platform_judge_config import (
    build_platform_judge_config_router,
)
from control_plane.api.platform_quality_config import (
    build_platform_quality_config_router,
)
from control_plane.api.platform_skills import build_platform_skills_router
from control_plane.api.platform_tool_budget_config import (
    build_platform_tool_budget_config_router,
)
from control_plane.api.quality import build_quality_router
from control_plane.api.quota import build_quota_router
from control_plane.api.rate_card import build_rate_card_router
from control_plane.api.role_bindings import build_role_bindings_router
from control_plane.api.runs import build_runs_list_router, build_runs_router
from control_plane.api.sandbox_egress_audit import build_sandbox_egress_audit_router
from control_plane.api.sandboxes import build_sandboxes_router
from control_plane.api.service_accounts import build_service_accounts_router
from control_plane.api.sessions import build_sessions_router
from control_plane.api.setup import build_setup_router
from control_plane.api.skill_evolution import build_skill_evolution_router
from control_plane.api.skills import build_skills_router
from control_plane.api.tenant_config import build_tenant_config_router
from control_plane.api.tenant_quotas import build_tenant_quotas_router
from control_plane.api.tenants import build_tenants_router
from control_plane.api.triggers import build_triggers_router, build_webhooks_router
from control_plane.api.uploads import build_uploads_router
from control_plane.api.usage import build_usage_router
from control_plane.api.webhook_endpoints import build_webhook_endpoints_router
from control_plane.api.workspace import build_workspace_router

__all__ = [
    "build_agent_schema_router",
    "build_agent_templates_router",
    "build_agent_users_router",
    "build_agents_router",
    "build_api_keys_router",
    "build_approvals_router",
    "build_artifacts_router",
    "build_audit_router",
    "build_billing_admin_router",
    "build_conversations_router",
    "build_curation_router",
    "build_eval_dataset_router",
    "build_eval_runs_router",
    "build_external_approvals_router",
    "build_external_events_router",
    "build_external_runs_router",
    "build_external_sessions_router",
    "build_external_uploads_router",
    "build_external_workspace_router",
    "build_feedback_router",
    "build_health_router",
    "build_knowledge_router",
    "build_mcp_catalog_router",
    "build_mcp_oauth_router",
    "build_mcp_servers_router",
    "build_me_router",
    "build_members_router",
    "build_memory_router",
    "build_metrics_router",
    "build_model_catalog_router",
    "build_plan_router",
    "build_platform_billing_config_router",
    "build_platform_config_router",
    "build_platform_delegation_config_router",
    "build_platform_dynamic_worker_config_router",
    "build_platform_embedding_config_router",
    "build_platform_judge_config_router",
    "build_platform_quality_config_router",
    "build_platform_skills_router",
    "build_platform_tool_budget_config_router",
    "build_quality_router",
    "build_quota_router",
    "build_rate_card_router",
    "build_role_bindings_router",
    "build_runs_list_router",
    "build_runs_router",
    "build_sandbox_egress_audit_router",
    "build_sandboxes_router",
    "build_service_accounts_router",
    "build_sessions_router",
    "build_setup_router",
    "build_skill_evolution_router",
    "build_skills_router",
    "build_tenant_config_router",
    "build_tenant_quotas_router",
    "build_tenant_users_router",
    "build_tenants_router",
    "build_triggers_router",
    "build_uploads_router",
    "build_usage_router",
    "build_webhook_endpoints_router",
    "build_webhooks_router",
    "build_workspace_router",
]
