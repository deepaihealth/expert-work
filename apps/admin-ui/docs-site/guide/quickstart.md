# 1 概述与对接流程

本篇带你五分钟跑通第一次对接:拿到 API Key,发一次请求,看到 Agent 的回答以 SSE 流的形式返回。

## 第一步:拿到 API Key

调用 Expert-Work 的开放 API 需要一个 `aforge_pat_` 开头的 Bearer key。Key 由你的租户管理员在控制台为一个"服务账号"创建——创建方式、scope 怎么选、如何轮换,见 [认证](./auth)。

拿到 key 后,把它放进每次请求的 `Authorization` 头:

```
Authorization: Bearer aforge_pat_xxxxx_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

## 第二步:发起一次调用

一次 `POST /v1/agents/{agent_code}/runs` 调用会:铸造(或复用)你这边的终端用户、绑定/续接一段会话、跑一次 Agent,然后把回答通过 SSE 流回来。`{agent_code}` 是你租户里已发布(ACTIVE)的 Agent 名字。

```bash
curl -N -X POST https://<your-domain>/v1/agents/{agent_code}/runs \
  -H "Authorization: Bearer <key>" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "u-123",
    "input": "你好",
    "mode": "stream"
  }'
```

- `-N` 关闭 curl 的输出缓冲,这样能实时看到流式帧,而不是等请求结束后一次性吐出来。
- `user_id` 是你自己业务系统里这个终端用户的标识(取值建议见 [最佳实践](./best-practices)),必填。
- `mode` 默认就是 `"stream"`,这里显式写出来。

## 第三步:读懂返回

请求成功会得到 `200`,`Content-Type: text/event-stream`,响应头里带着 `X-Expert-Work-Session-Id`(这次对话的 session id,记下来,续聊要用)和 `X-Expert-Work-Run-Id`。响应体是一串 SSE 帧:

```
event: metadata
data: {"run_id":"...","thread_id":"..."}

event: updates
data: {...}

event: end
data: {"status":"success","run_id":"..."}
```

`end` 帧的 `status` 就是这次 run 的终局状态,四个取值:`success`(答完了)/ `paused`(停在人工审批节点等你决策,**不是失败**)/ `interrupted`(被中断)/ `error`(失败,超时也算这一档)。

可回放的帧还会多一行 `id: {毫秒时间戳}-{seq}`(`end` / `token` 这类帧没有)——断线重连时要把见过的最大 `seq` 传回去,服务端只补它之后的帧。

完整的事件类型、字段含义、断线重连怎么处理,见 [SSE 事件格式](./sse-events)。

## 续聊

把上一次拿到的 `X-Expert-Work-Session-Id` 作为下次请求 body 里的 `session_id` 传回去,就能在同一段会话里继续对话;不传就是开一段新会话。完整参数表见 [调用 Agent](./run-agent)。
