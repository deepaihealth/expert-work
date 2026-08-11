# 认证

本篇讲清楚:服务账号和 API Key 的关系、key 长什么样、三档 scope 怎么选、以及轮换/过期怎么处理。

## 服务账号与 Key:持钥人与钥匙

"服务账号"(service account)是租户下一个长期存在的程序身份——持钥人。每个服务账号下可以挂 0 个或多个 API Key——钥匙。调用方拿着钥匙(key)证明自己是这个持钥人,但服务账号本身不能直接发请求,必须靠 key。

一个服务账号可以同时有多把 key(比如一把旧 key 和一把刚轮换出来的新 key 同时有效),吊销其中一把不影响同一服务账号下的其它 key。

## Key 长什么样

Key 的完整明文格式是:

```
aforge_pat_<5位十六进制>_<32位随机串>
```

例如 `aforge_pat_a1b2c_XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX`(示例,非真实值)。开头 5 位十六进制取自租户 id 的前 5 个字符,方便你在日志/审计面板里快速识别这把 key 属于哪个租户,但不会泄露完整租户 id。

**明文只会在创建(或轮换)时返回一次。** 服务端只存 key 的前 25 位字符(用于查找)加上完整明文的 argon2id 哈希——数据库里从来没有完整明文。拿到明文后立刻保存到你自己的密钥管理系统;之后只能靠 `POST /v1/api_keys/{id}/reveal` 重新查看,且仅对创建/轮换时已支持回显的 key 有效——更早创建的 key 拿不回明文,只能轮换出一把新的。

## Scope 怎么选

创建 key 时要选 0 个或多个 scope(`read` / `write` / `admin`):

| scope | 能做什么 | 什么时候给 |
|---|---|---|
| `read` | 只读——查会话/run 状态、拉配额信息等 | 只需要轮询 run 状态或重放 SSE 事件(比如 `queue` 模式的轮询),不需要发起新 run |
| `write` | 在 `read` 之上,可以创建/推进会话、发起 run | 调用 `POST /v1/agents/{agent_code}/runs` 必须要有它——接口要求调用方具备 session 资源的写权限 |
| `admin` | 租户内几乎所有资源的读写删,包括创建/轮换/吊销其它 API Key、读密钥(secret)、改 role binding | **永远不要发给第三方对接方**——这档 scope 能让持有者给自己或别人再发新 key、读密钥,一旦泄露等于交出整个租户 |

实操建议:发给外部集成方的 key 只给 `write`(需要轮询就加上 `read`)——够用来发起 run、查状态,不多给。`admin` 只留给你自己团队内部管理这批 key 的场景。

管理 key 本身(创建/轮换/吊销)的接口需要调用方具备 `api_key` 资源的写权限,而这只有租户 `admin` 角色拥有——也就是说这些操作通常是你的租户管理员在控制台里做,不是拿对接方自己的 key 去做。

## 创建一把 Key

```bash
curl -X POST https://<your-domain>/v1/service_accounts/{service_account_id}/api_keys \
  -H "Authorization: Bearer <管理员的凭证>" \
  -H "Content-Type: application/json" \
  -d '{"scopes": ["write"], "expires_at": null}'
```

返回:

```json
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

`plaintext` 就是要放进 `Authorization: Bearer` 头的那个值——只显示这一次。

## 过期语义

`expires_at` 可以不传 / 传 `null`,含义是**永不过期**。除非有明确理由要一把永久 key,否则建议显式设置一个过期时间,到期前用轮换(见下)换一把新的——长期不过期的 key 一旦泄露,暴露窗口是无限的。

## 轮换与吊销

**轮换**(`POST /v1/api_keys/{api_key_id}/rotate`)是给同一个服务账号换一把新钥匙,同时给旧钥匙留一段"双活"宽限期:

```bash
curl -X POST https://<your-domain>/v1/api_keys/{api_key_id}/rotate \
  -H "Authorization: Bearer <管理员的凭证>" \
  -H "Content-Type: application/json" \
  -d '{"grace_period_s": 300}'
```

- `grace_period_s` 默认 300 秒(5 分钟),最大 3600 秒。宽限期内旧 key 和新 key 都能验证通过——给你的服务留出把配置换成新 key 的时间窗口,不用停机切换。
- 宽限期一过,旧 key 立刻失效(401)。
- 想立即作废、不留宽限期?直接调 `DELETE /v1/api_keys/{api_key_id}` 吊销,不走轮换流程。

## Key 失效时会发生什么

不合法、已吊销、已过期、或者宽限期已过的旧 key,调用任何接口都会拿到 `401`——具体错误码和文案见 [错误码与限流](./errors)。
