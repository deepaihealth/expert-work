# Task 4 报告:MCP 引用检查修缮(D3 + 假 409 bug 修)—— deletion-hygiene PR4

STATUS: DONE

## 交付

### ① 假 409 bug 修(必修)
`services/control-plane/src/control_plane/api/mcp_servers.py` delete 端点引用块:
`list_by_tenant(status=None)` 会返回含 `DELETED` 墓碑行的全部 spec(in-memory 与 SQL 皆然),
软删 agent 的显式引用因此永久锁死 server 删除。已按 brief 加过滤谓词
`s.status is not AgentSpecStatus.DELETED`(DEPRECATED 仍算 active 引用面),409 detail 原样不动。

### ② D3 留空影响面提示
- 新 helper `manifest_uses_implicit_all(manifest: dict[str, object], /) -> bool`
  (mcp_servers.py:73,签名/docstring 逐字用 brief 的):存在 `type=="mcp"` 且 `servers`
  缺失(pre-V-E)或 `[]` 的工具条目 → True;显式列表 → False;无 mcp 工具/malformed → False。
- 删除成功路径统计该租户 active spec 的隐式全部数,进删除响应 `data.implicit_all_agents`
  与 MCP_SERVER_DELETE 审计 `details.implicit_all_agents`。
- `model_dump(mode="json")` 每 spec 只调一次(`dumped` 局部变量,引用检查与 implicit 计数复用)。

## ⚠️ 与 brief 字面的一处偏差(有意,请复核)

brief 测试①③写"删除 **204**",但④同时要求 `implicit_all_agents` 进"删除成功响应 data"
——204 按 HTTP 语义不能带 body(ASGITransport 测试下会假通过,真栈 uvicorn/h11 下发 body
会炸)。设计文档 D3 行(用户拍板)明确"删除响应/审计带计数",故取响应体为准:
**DELETE 从 204/None 改为 200 + 标准信封** `{"success": true, "data": {"implicit_all_agents": N}, "error": null}`
(照同文件 PATCH/`DELETE catalog enable` 先例)。影响面已核:
- 前端 `apps/admin-ui/src/api/mcp-servers.ts:121` `deleteMcpServer` 返 `Promise<void>`
  且不读 body/status(axios 2xx 即成功)→ 零前端改动。
- e2e/docs/SDK 无对该端点 204 的硬编码。
- 既有 4 处 `== 204` 断言随行为改为 `== 200`(同文件 docstring 里的 204 描述同步改)。

## TDD 记录

- **RED**:helper 单测文件 import 新 helper → 收集期 ImportError;API 侧 6 红:
  哨兵①红原因确认为真 409(`{"code":"MCP_SERVER_IN_USE","message":"referenced by agent(s): ghost"}`),
  implicit 计数测试红(204 无 body),4 个既有 delete 测试红在 204→200 契约。
  中途一修:seed helper 的 `spec_sha256` 需 64 位 hex(AgentSpecRecord 校验),改用真 sha256。
- **GREEN**:两文件 48 全绿。

## 变异自验 ×2(Global Constraints)

| 变异 | 期望红 | 实测 |
|---|---|---|
| A:去 status 过滤(`active_specs = list(specs)`) | 哨兵① | `test_delete_ignores_soft_deleted_agent_reference` 红 + `test_delete_reports_implicit_all_agents_in_response_and_audit` 红(软删 wildcard 被计入 3≠2)✅ |
| B:`manifest_uses_implicit_all` 判定永假 | 测试④ | `test_delete_reports_implicit_all_agents_in_response_and_audit` 红 + 2 个 helper 单测红 ✅ |

两次变异后均已恢复,终态 48/48 绿。

## 新增/修改测试

`tests/test_mcp_server_reference_check.py`(+7 用例):
`servers=[]` 不算硬引用(补缺失用例③)/ implicit-all 空列表 True / 缺失 True /
显式 False / 无 mcp 工具 False / malformed False。

`tests/test_mcp_servers_api.py`(+4 端到端 + seed helpers):
- `test_delete_ignores_soft_deleted_agent_reference` — bug 回归哨兵①
- `test_delete_conflicts_when_active_agent_references` — ② 409 照旧 + 行存活
- `test_delete_conflicts_when_deprecated_agent_references` — 谓词方向护栏(防误写成 `is ACTIVE`)
- `test_delete_reports_implicit_all_agents_in_response_and_audit` — ④ N=2
  (2 live wildcard + 1 无 mcp 计 0 + 1 软删 wildcard 不计),响应 data 与审计 details 双断言
- 既有 4 测 204→200,unreferenced 测试加 `implicit_all_agents == 0` 零值断言

副作用不进 assert(审计断言走 InMemoryAuditLogStore.query,照既有套路);
日志无新增(未动 (c2) 密文清理块,PR2 的 :1031+ 原样)。

## 验证

- `uv run pytest tests/test_mcp_server_reference_check.py tests/test_mcp_servers_api.py` → 48 passed
- MCP 相关全文件(catalog/oauth/runtime_mcp 共 6 文件)→ 100 passed
- `uv run ruff check` 全库 → All checks passed;`ruff format --check` → 干净
- CI 同款 `uv run mypy packages …/src` → Success (783 files)
