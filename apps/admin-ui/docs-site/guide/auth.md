# 6 认证与 Key

所有对外接口都用 `Authorization: Bearer <key>` 认证。本章说明 key 的来源、格式、权限档位，以及轮换与吊销的方式。

## 6.1 服务账号与 API Key

服务账号（service account）是租户下一个长期存在的程序身份，代表「哪个系统在调用」。API Key 是它的凭证。

```mermaid
flowchart LR
    T[租户] --> SA1[服务账号 A<br/>例如：订单系统]
    T --> SA2[服务账号 B<br/>例如：客服系统]
    SA1 --> K1[Key 1<br/>使用中]
    SA1 --> K2[Key 2<br/>轮换出的新 key]
    SA2 --> K3[Key 3]
```

服务账号本身不能直接发请求，必须使用 key。一个服务账号可以同时挂多把有效的 key，例如一把旧 key 和一把刚轮换出来的新 key；吊销其中一把不影响同一账号下的其它 key。

## 6.2 Key 的格式

``` [Key 的格式]
aforge_pat_<hex>_<random>
```

例如 `aforge_pat_a1b2c_XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX`（示例，非真实值）。`<hex>` 是 5 位十六进制，取自租户 id 的前 5 个字符，便于在日志里快速识别这把 key 属于哪个租户，同时不会泄露完整的租户 id。`<random>` 是 32 位随机字符串。

::: warning 明文只在创建或轮换时返回一次
服务端只保存 key 的前 25 位字符（用于查找）和完整明文的 argon2id 哈希，数据库里没有完整明文。

取得明文后请立即存入调用方自己的密钥管理系统。之后只能通过 `POST /v1/api_keys/{id}/reveal` 重新查看，且仅对创建或轮换时已支持回显的 key 有效；更早创建的 key 无法取回明文，只能轮换出一把新的。
:::

## 6.3 权限档位

创建 key 时选择 0 个或多个权限档位（`read` / `write` / `admin`）：

| 档位 | 能做什么 | 什么时候给 |
|---|---|---|
| `read` | 只读：Agent 目录、会话列表与历史消息、run 列表与状态、按 run 拉取事件（断线后可续传，见 [3.6 断线重连](./sse-events#_3-6-断线重连与回放分页)）、工作区文件列表与下载、附件下载、产物列表与下载 | 只查询、不发起新 run 的纯只读集成 |
| `write` | 在 `read` 之上，可以提前获取 session_id、发起 run、取消 run、审批决策、上传附件、重命名与归档会话、删除产物 | 对接方的默认选择。[发起对话](./chat) 与 [取消与审批](./run-control) 都要求这一档 |
| `admin` | 租户内资源的删除权。对外接口不需要这一档 | 对外接口用不到，不发给对接方 |

**给对接方的 key 选 `write` 一档即可**，它已经包含 `read` 的全部只读能力；纯只读的集成选 `read`。`admin` 不会让对外接口多出任何能力（删除产物要求的是 `write`），也管不了 key 本身（管理 key 需要租户管理员的登录凭证），没有理由发给对接方。

### API Key 不能访问租户管理面

Key 只能访问第三方对接接口（`/v1/agents/{agent_code}/…` 与 `/v1/agent-catalog`）。租户管理面的能力——管理 key 本身、成员名册、操作流水、租户配置与配额、MCP 注册表、长期记忆——全部拒绝 API Key，返回 403：

```json [响应 403]
{"detail": {"code": "FORBIDDEN",
            "message": "console API is not available to API keys; use /v1/agents/{agent_code}/…"}}
```

这与权限档位无关，`admin` 档的 key 同样被拒绝；档位只在对接接口内部区分能做什么。上面这些操作需要租户管理员本人登录后的凭证：在控制台里完成，或用管理员登录凭证调用管理接口（用法见 [6.4 创建 Key](#_6-4-创建-key)），不能用对接方的 key。

## 6.4 创建 Key

由租户管理员执行。`{admin_jwt}` 是租户管理员登录后取得的凭证，不能用 API Key 代替：

```bash [请求]
curl -X POST https://<your-domain>/v1/service_accounts/{service_account_id}/api_keys \
  -H "Authorization: Bearer {admin_jwt}" \
  -H "Content-Type: application/json" \
  -d '{"scopes": ["write"], "expires_at": null}'
```

```json [响应 200]
{
  "success": true,
  "data": {
    "api_key": {
      "id": "...",
      "prefix": "aforge_pat_a1b2c_...",
      "scopes": ["write"],
      "expires_at": null,
      "created_at": "..."
    },
    "plaintext": "aforge_pat_a1b2c_XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"
  },
  "error": null
}
```

`plaintext` 就是要放进 `Authorization: Bearer` 的取值，只返回这一次。

## 6.5 过期时间

`expires_at` 可以不传或传 `null`，含义是永不过期。

除非有明确理由，否则建议显式设置一个过期时间并在到期前轮换。长期不过期的 key 一旦泄露，暴露窗口没有尽头。

## 6.6 轮换与吊销

轮换是给同一个服务账号换一把新 key，同时给旧 key 留一段两者都有效的宽限期，让调用方不停机切换配置。

```mermaid
sequenceDiagram
    autonumber
    participant A as 租户管理员
    participant E as Expert-Work
    participant S as 调用方服务端

    A->>E: POST /v1/api_keys/{id}/rotate { grace_period_s: 300 }
    E-->>A: 新 key 明文（只返回一次）
    A->>S: 配置更新为新 key
    Note over E: 宽限期内：新旧 key 都验证通过
    Note over E: 宽限期结束：旧 key 立即 401
```

```bash [请求]
curl -X POST https://<your-domain>/v1/api_keys/{api_key_id}/rotate \
  -H "Authorization: Bearer {admin_jwt}" \
  -H "Content-Type: application/json" \
  -d '{"grace_period_s": 300}'
```

- `grace_period_s` 默认 300 秒（5 分钟），可取 0 到 3600 秒；传 0 表示不留宽限期。
- 宽限期结束后，旧 key 立即失效，返回 401。
- 需要立即作废、不留宽限期时，直接调 `DELETE /v1/api_keys/{api_key_id}` 吊销，不走轮换。**怀疑 key 泄露时使用吊销，不要等宽限期。**

## 6.7 Key 失效后的行为

不合法、已吊销、已过期，或宽限期已过的旧 key，调用任何接口都返回 401。具体错误码与响应格式见 [8.4 401 认证失败](./errors#_8-4-401-认证失败)。
