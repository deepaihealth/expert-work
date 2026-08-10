# API 密钥可回显 + 密钥页清理 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** ①API 密钥明文加密存平台金库,支持在列表反复查看(带审计);②创建弹窗 scope 竖排+三档说明+过期留空提示;③删掉 SettingsApiKeys 页面残留的本地四项侧栏菜单。

**Architecture:** 创建/轮换时把明文 `put` 进既有 `SecretStore`(sql_encrypted 金库,LLM 凭证同款),吊销时 `delete`;新增 `POST /v1/api_keys/{id}/reveal` 解密回显(`require("api_key","write")` 同创建门槛)+ 新 `AuditAction.API_KEY_REVEAL` 审计。哈希校验路径**一字不动**(网关验证仍走 argon2id,金库只服务回显)。前端列表加「查看」,show-once 弹窗文案改为「可随时在列表中再次查看」。

**Tech Stack:** FastAPI、SecretStore(`expert_work.runtime.secret_store`)、React + antd、pytest / vitest。

## Global Constraints

- **验证路径零改动**:`ApiKeyVerifier` 与 `api_key` 表的 prefix+hash 逻辑不碰——金库明文只用于回显,验证仍走哈希。
- 金库名约定:`expert-work/tenant/{tenant_id}/api-key/{api_key_id}`(照 `secret://expert-work/platform/…` 既有命名族)。
- 明文**只**出现在:金库(加密)、create/rotate/reveal 三个响应体。绝不进日志、绝不进 audit details、绝不进列表 GET 响应。
- 每次 reveal 落一条 `AuditAction.API_KEY_REVEAL = "api_key:reveal"` 审计(actor、key id;不含明文)。
- 旧密钥(金库无明文,本功能上线前创建的)reveal 返回 404 `{"code": "API_KEY_PLAINTEXT_UNAVAILABLE", "message": "key predates reveal support; rotate to get a viewable one"}`。
- 金库写失败不阻断创建(密钥本体已发出,拿 warning log + 响应正常返回;那把 key 表现同旧密钥)。
- revoke(DELETE)与 rotate 的旧 key 都要 `delete` 金库明文(rotate 存新的);delete 失败只 warning。
- 变异自证仓库铁律:每条新断言 break→red→restore→green;重点杀「reveal 忘记审计」「明文进列表响应」「revoke 不清金库」。
- i18n 新键 en+zh-CN 同步,先查重。
- 本地验证:`cd services/control-plane && uv run pytest tests/test_api_keys.py -q`(文件名以真实为准);`cd apps/admin-ui && npx vitest run src/pages/__tests__/SettingsApiKeys.test.tsx && npx tsc --noEmit`。

## File Structure

- `packages/expert-work-protocol/src/expert_work/protocol/audit.py` — `API_KEY_REVEAL` 枚举(改)
- `services/control-plane/src/control_plane/api/api_keys.py` — create/rotate 存金库、revoke 清金库、新 reveal 端点(改)
- `services/control-plane/tests/`(api_keys 测试文件,改)
- `apps/admin-ui/src/api/api_keys.ts` — reveal 绑定(改)
- `apps/admin-ui/src/pages/SettingsApiKeys.tsx` — 查看按钮+弹窗、show-once 文案、删本地侧栏、创建弹窗改版(改)
- `apps/admin-ui/src/i18n/locales/{en,zh-CN}.ts`(改)

---

### Task 1: 后端——金库存取 + reveal 端点 + 审计

**Files:**
- Modify: `packages/expert-work-protocol/src/expert_work/protocol/audit.py`(`API_KEY_ROTATE` 之后加 `API_KEY_REVEAL = "api_key:reveal"`)
- Modify: `services/control-plane/src/control_plane/api/api_keys.py`
- Test: control-plane 里 api_keys 的既有测试文件(先 `grep -rl "api_keys" services/control-plane/tests/` 找到)

**Interfaces:**
- Produces: `POST /v1/api_keys/{api_key_id}/reveal` → 200 `{"success": true, "data": {"plaintext": "..."}, "error": null}`(信封照本文件既有端点);404 `API_KEY_NOT_FOUND` / 404 `API_KEY_PLAINTEXT_UNAVAILABLE`。金库名 helper `_vault_name(tenant_id, api_key_id) -> str`。

- [ ] **Step 1: 写失败测试**(fixture 照既有 api_keys 测试;secret store 用 app 里已装配的 memory 实现)

```python
@pytest.mark.asyncio
async def test_create_then_reveal_roundtrip(client, ...):
    created = await client.post(".../api_keys", json={"scopes": ["read"]})
    key_id = created.json()["data"]["id"]
    plaintext = created.json()["data"]["plaintext"]
    revealed = await client.post(f"/v1/api_keys/{key_id}/reveal")
    assert revealed.status_code == 200
    assert revealed.json()["data"]["plaintext"] == plaintext


@pytest.mark.asyncio
async def test_reveal_is_audited(client, audit_store, ...):
    # create + reveal 后,audit 里有 action == "api_key:reveal" 的行,
    # 且全部审计行序列化 JSON 不含明文子串。


@pytest.mark.asyncio
async def test_reveal_predates_vault_404(client, ...):
    # 手工造一条金库里没有明文的 key 行(直接走 store 建行,不走 create 端点),
    # reveal → 404,error.code == "API_KEY_PLAINTEXT_UNAVAILABLE"。


@pytest.mark.asyncio
async def test_revoke_clears_vault(client, secret_store, ...):
    # create → revoke → 金库 get 该名字抛 SecretNotFoundError(或该实现的等价异常)。


@pytest.mark.asyncio
async def test_rotate_stores_new_plaintext(client, ...):
    # create → rotate → reveal 返回的是 rotate 响应里的新明文,不是旧的。


@pytest.mark.asyncio
async def test_list_response_has_no_plaintext(client, ...):
    # create 后 GET /v1/api_keys,序列化整个响应断言不含明文子串。
```

(注释体测试全部写实,不许留空。)

- [ ] **Step 2: 跑测试确认失败**

- [ ] **Step 3: 实现**

api_keys.py:

```python
def _vault_name(tenant_id: UUID, api_key_id: UUID) -> str:
    return f"expert-work/tenant/{tenant_id}/api-key/{api_key_id}"


def _get_secret_store(request: Request) -> SecretStore:
    return request.app.state.secret_store  # type: ignore[no-any-return]
```

- create 端点:发 key 成功后 `await secrets.put(_vault_name(...), generated.plaintext)`,`except Exception: logger.warning("api_key.vault_put_failed api_key_id=%s", ...)`(不含明文,不回滚)。
- rotate 端点:同样对**新** key `put`;旧 key 若换 id 则 `delete` 旧名(失败 warning);同 id 则 put 覆盖即可(读现有 rotate 实现定夺,报告里写清是哪种)。
- revoke(DELETE)端点:`await secrets.delete(_vault_name(...))`,`except Exception: warning`。
- reveal 端点:

```python
@router.post("/v1/api_keys/{api_key_id}/reveal")
async def reveal_api_key(
    api_key_id: UUID,
    principal: Annotated[Principal, Depends(require("api_key", "write"))],
    keys: Annotated[ApiKeyStore, Depends(_get_keys)],
    secrets: Annotated[SecretStore, Depends(_get_secret_store)],
    audit: Annotated[AuditLogger, Depends(_get_audit)],
) -> dict[str, object]:
    record = await keys.get(tenant_id=principal.tenant_id, api_key_id=api_key_id)  # 名字照真实 store
    if record is None:
        raise HTTPException(status_code=404, detail={"code": "API_KEY_NOT_FOUND"})
    try:
        plaintext = await secrets.get(_vault_name(principal.tenant_id, api_key_id))
    except Exception as exc:  # SecretNotFound 一类——读真实异常类型收窄
        raise HTTPException(
            status_code=404,
            detail={
                "code": "API_KEY_PLAINTEXT_UNAVAILABLE",
                "message": "key predates reveal support; rotate to get a viewable one",
            },
        ) from exc
    await emit(audit, tenant_id=principal.tenant_id, actor_id=principal.subject_id,
               action=AuditAction.API_KEY_REVEAL, resource_type="api_key",
               resource_id=str(api_key_id), trace_id=current_trace_id_hex())
    return {"success": True, "data": {"plaintext": plaintext}, "error": None}
```

(`except Exception` 落码前先看金库实现抛什么,能收窄就收窄到具体异常;emit 的调用形状照本文件既有端点。)

- [ ] **Step 4: 全量 api_keys 测试绿**

- [ ] **Step 5: 变异自证**:①reveal 里删掉 emit → red(audited 测试)→ 还原;②list 响应塞 plaintext 字段 → red → 还原;③revoke 删掉 secrets.delete → red → 还原。

- [ ] **Step 6: Commit**

```bash
git add -A packages/expert-work-protocol services/control-plane
git commit -m "feat: API 密钥可回显——明文进加密金库 + reveal 端点 + 审计"
```

---

### Task 2: 前端——查看按钮 + show-once 文案 + 创建弹窗改版 + 删侧栏

**Files:**
- Modify: `apps/admin-ui/src/api/api_keys.ts`(加 `revealApiKey(id): Promise<{plaintext: string}>`)
- Modify: `apps/admin-ui/src/pages/SettingsApiKeys.tsx`
- Modify: `apps/admin-ui/src/i18n/locales/{en,zh-CN}.ts`
- Test: `apps/admin-ui/src/pages/__tests__/SettingsApiKeys.test.tsx`(如无则新建)

**Interfaces:**
- Consumes: Task 1 的 reveal 端点(404 code `API_KEY_PLAINTEXT_UNAVAILABLE` 要给专属文案:「该密钥创建于回显功能上线前,轮换后可查看」)。

四件事:

1. **删本地侧栏**:`SETTINGS_MENU` 常量、`<Sider>` 及其 Layout 包装整体拿掉,内容区占满(其他设置页没有这个侧栏,全局导航已有四项入口)。
2. **创建弹窗改版**:scope 竖排(去 Row/Col 两列网格),每行 Checkbox + `code` 样式 scope 名 + 灰色说明(`Text type="secondary"` fontSize 12,放 label 内点击可勾选):
   - read:「只读:查询会话、运行结果、Agent 列表等 GET 接口」
   - write:「业务写:调用 Agent 运行、创建/继续会话、上传文件(不含只读查询)」
   - admin:「管理:服务账号与密钥管理、授权变更——不要发给第三方」(保留红色危险 Tag)
   en 对应翻译;expiresAt Form.Item 加 `extra`:「留空 = 永不过期;对外分发的密钥建议设置有效期」。
3. **列表「查看」**:操作列加查看按钮 → 调 reveal → Modal 展示明文(`Text code copyable`)+ 复制;404 `API_KEY_PLAINTEXT_UNAVAILABLE` → `message.info` 专属文案;revoked/expired 行不显示查看按钮。
4. **show-once 弹窗与列表脚注文案**:「窗口关闭后无法再次查看完整密钥」改为「之后可在列表中点击『查看』再次获取完整密钥」(en 同步);`show_once_title` 等键名不动,只改文案值。

- [ ] **Step 1: 写失败测试**:①打开创建弹窗,三条 scope 说明文字都在;②列表行点查看(mock reveal 200)→ Modal 里出现明文;③mock reveal 404 `API_KEY_PLAINTEXT_UNAVAILABLE` → 出现专属提示文案;④页面不再渲染 "Service Accounts" 侧栏文本。
- [ ] **Step 2: 跑测试红**
- [ ] **Step 3: 实现**(antd Modal/message 全走 `App.useApp()`;明文零持久化)
- [ ] **Step 4: vitest 该文件全绿 + 全量 `npx vitest run` 无回归 + `npx tsc --noEmit` 干净**
- [ ] **Step 5: 变异自证**:查看 Modal 不渲染明文 → red → 还原;删一条 scope 说明 → red → 还原。
- [ ] **Step 6: Commit**

```bash
git add -A apps/admin-ui
git commit -m "feat(admin-ui): API 密钥可反复查看 + 创建弹窗三档说明 + 删残留侧栏"
```
