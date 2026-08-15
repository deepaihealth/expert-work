# 8 附录:多语言示例

本章给八个常见场景各配一份完整可跑的最小示例(curl + Python;Node.js / Java 后续补齐)。几条通用约定:

- **key 一律从环境变量读,示例代码里绝不出现明文 key。** 运行前自己设好:

  ```bash
  export EXPERT_WORK_API_KEY="aforge_pat_..."
  ```

  key 长什么样、怎么申请,见 [认证](./auth)。
- **域名统一写成 `https://<your-domain>`**,替换成你实际对接的地址,见 [通用约定](./conventions) 的「环境地址」。
- `{agent_code}` / `{run_id}` / `{session_id}` 这类花括号占位符,替换成你自己的真实值。
- 请求 / 响应字段含义见 [调用 Agent](./run-agent) 与 [取消 run 与审批决策](./run-control);SSE 帧含义见 [SSE 事件格式](./sse-events)。

## 8.1 发起 run(stream)并解析 SSE

发一次 `mode: "stream"` 的 run,响应体本身就是 SSE 流。第三方最容易在这一步踩坑的是 SSE 分帧——下面 Python 示例的注释里标出了必须处理对的四件事。

::: code-group

```bash [curl]
curl -N -X POST "https://<your-domain>/v1/agents/{agent_code}/runs" \
  -H "Authorization: Bearer ${EXPERT_WORK_API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "u-123",
    "input": "你好",
    "mode": "stream"
  }'
```

```python [Python]
import json
import os
import urllib.request

API_KEY = os.environ["EXPERT_WORK_API_KEY"]
BASE_URL = "https://<your-domain>"
AGENT_CODE = "{agent_code}"  # 替换成你的 agent_code


def iter_sse_frames(response):
    """
    从响应里边读边攒 buffer,按空行("\n\n")分帧——①不是按行分帧。
    一帧可能有 id / event / data 好几行,按行处理会把一帧拆散、data 里的换行也会被切碎。
    """
    buffer = ""
    while True:
        chunk = response.read(1024)
        if not chunk:
            return
        buffer += chunk.decode("utf-8")
        while "\n\n" in buffer:
            raw_frame, buffer = buffer.split("\n\n", 1)
            yield raw_frame


def parse_frame(raw_frame):
    event = None
    seq = None
    data_lines = []
    for line in raw_frame.split("\n"):
        if line.startswith(":"):
            # ②以 ":" 开头的是心跳注释行(服务端约 15 秒发一条),不是事件,跳过
            continue
        if line.startswith("event:"):
            event = line[len("event:"):].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:"):].strip())
        elif line.startswith("id:"):
            # ③id 形如 "{毫秒时间戳}-{seq}",按最后一个 "-" 切分取 seq
            #   (前半段时间戳本身不含 "-",但统一按"最后一个"切更保险)
            seq = int(line[len("id:"):].strip().rsplit("-", 1)[-1])
    data = json.loads("\n".join(data_lines)) if data_lines else None
    return event, data, seq


def run_and_stream(user_id, input_text):
    url = f"{BASE_URL}/v1/agents/{AGENT_CODE}/runs"
    body = json.dumps({"user_id": user_id, "input": input_text, "mode": "stream"}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
        },
    )

    max_seq_seen = None  # 维护"见过的最大 seq",断线重连时当游标用(完整重连示例见 8.5)

    with urllib.request.urlopen(req) as response:
        run_id = response.headers.get("X-Expert-Work-Run-Id")

        for raw_frame in iter_sse_frames(response):
            if not raw_frame.strip():
                continue  # 心跳可能在缓冲区里留下一个空帧,跳过
            event, data, seq = parse_frame(raw_frame)
            if seq is not None:
                max_seq_seen = seq if max_seq_seen is None else max(max_seq_seen, seq)

            if event == "end":
                # ④收到 end 才算结束;end 帧带这次 run 的终局 status
                print("run 结束,status =", data["status"])
                break
            elif event == "truncated":
                # ④收到 truncated 不算结束——这一页装不下,要带 next_seq 继续拉(见 8.5)
                print("这一页被截断,next_seq =", data["next_seq"])
                continue
            elif event is not None:
                # metadata / updates / approval / retry / error,以及未来可能新增的类型
                print(event, data)

    return run_id


if __name__ == "__main__":
    run_and_stream("u-123", "你好")
```

:::

## 8.2 queue 模式 + 轮询结果

`mode: "queue"` 立即返回 `202`,不建流。202 响应体的 `data.thread_id` 就是这次绑定的 `session_id`(字段名不一样,值是同一个)。用 `GET /v1/agents/{agent_code}/sessions` 里每一项的 `running` 字段粗粒度轮询,`running` 变成 `false` 后再去拉历史消息拿最终回答。

::: code-group

```bash [curl]
# 发起 queue 模式的 run
curl -X POST "https://<your-domain>/v1/agents/{agent_code}/runs" \
  -H "Authorization: Bearer ${EXPERT_WORK_API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "u-123",
    "input": "帮我整理一份周报",
    "mode": "queue"
  }'
# → 202,data.thread_id 就是这次绑定的 session_id,记下来

# 轮询会话列表,看这个 session_id 对应项的 running 字段(thread_id 换成上一步拿到的值)
curl "https://<your-domain>/v1/agents/{agent_code}/sessions?user_id=u-123" \
  -H "Authorization: Bearer ${EXPERT_WORK_API_KEY}"

# running 变成 false 后,拉历史消息拿最终回答(session_id 换成 thread_id 的值)
curl "https://<your-domain>/v1/agents/{agent_code}/sessions/{session_id}/messages?user_id=u-123" \
  -H "Authorization: Bearer ${EXPERT_WORK_API_KEY}"
```

```python [Python]
import json
import os
import time
import urllib.parse
import urllib.request

API_KEY = os.environ["EXPERT_WORK_API_KEY"]
BASE_URL = "https://<your-domain>"
AGENT_CODE = "{agent_code}"  # 替换成你的 agent_code


def _get(url):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {API_KEY}"})
    with urllib.request.urlopen(req) as response:
        return json.loads(response.read().decode("utf-8"))


def start_queue_run(user_id, input_text):
    url = f"{BASE_URL}/v1/agents/{AGENT_CODE}/runs"
    body = json.dumps({"user_id": user_id, "input": input_text, "mode": "queue"}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return payload["data"]  # {"run_id": "...", "thread_id": "...", "status": "queued"}


def is_session_running(user_id, session_id):
    query = urllib.parse.urlencode({"user_id": user_id})
    url = f"{BASE_URL}/v1/agents/{AGENT_CODE}/sessions?{query}"
    payload = _get(url)
    for session in payload["data"]["sessions"]:
        if session["session_id"] == session_id:
            return session["running"]
    return False  # 查不到这个 session 时按未在跑处理


def poll_until_done(user_id, session_id, interval_s=2, max_attempts=30):
    for _ in range(max_attempts):
        if not is_session_running(user_id, session_id):
            return
        time.sleep(interval_s)
    raise TimeoutError("超过最大轮询次数,run 仍未结束")


def fetch_last_final_answer(user_id, session_id):
    query = urllib.parse.urlencode({"user_id": user_id})
    url = f"{BASE_URL}/v1/agents/{AGENT_CODE}/sessions/{session_id}/messages?{query}"
    payload = _get(url)
    for message in reversed(payload["data"]["messages"]):
        if message["role"] == "assistant" and message["channel"] == "final":
            return message["content"]
    return None


if __name__ == "__main__":
    result = start_queue_run("u-123", "帮我整理一份周报")
    session_id = result["thread_id"]  # queue 响应里字段名是 thread_id,续接时仍传给 session_id
    poll_until_done("u-123", session_id)
    print(fetch_last_final_answer("u-123", session_id))
```

:::

## 8.3 上传文件并带进 run

先调上传接口拿 `upload_id`,再原样放进 `files[]` 发起 run。下面示例传一份文档(`type: "document"`);图片走法一样,唯一区别是 `upload_id` 的格式(`expert_work://image/...`),细节见 [调用 Agent](./run-agent) 的「`files[]`」一节。

::: code-group

```bash [curl]
# 上传文件,拿 upload_id
curl -X POST "https://<your-domain>/v1/agents/{agent_code}/uploads" \
  -H "Authorization: Bearer ${EXPERT_WORK_API_KEY}" \
  -F "user_id=u-123" \
  -F "file=@report.pdf;type=application/pdf"
# → data.upload_id 形如 "uploads/report.pdf",原样回传,不要自己截取或改写

# 把 upload_id 放进 files[],发起 run(upload_id / session_id 换成上一步返回的值)
curl -X POST "https://<your-domain>/v1/agents/{agent_code}/runs" \
  -H "Authorization: Bearer ${EXPERT_WORK_API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "u-123",
    "session_id": "<上一步返回的 session_id>",
    "input": "帮我看看这份文件",
    "mode": "queue",
    "files": [
      { "type": "document", "transfer_method": "local_file", "upload_id": "uploads/report.pdf" }
    ]
  }'
```

```python [Python]
import json
import mimetypes
import os
import urllib.request
import uuid

API_KEY = os.environ["EXPERT_WORK_API_KEY"]
BASE_URL = "https://<your-domain>"
AGENT_CODE = "{agent_code}"  # 替换成你的 agent_code


def upload_file(user_id, file_path):
    boundary = uuid.uuid4().hex
    mime_type, _ = mimetypes.guess_type(file_path)
    mime_type = mime_type or "application/octet-stream"
    filename = os.path.basename(file_path)

    with open(file_path, "rb") as f:
        file_bytes = f.read()

    parts = [
        f"--{boundary}\r\n".encode("utf-8"),
        b'Content-Disposition: form-data; name="user_id"\r\n\r\n',
        f"{user_id}\r\n".encode("utf-8"),
        f"--{boundary}\r\n".encode("utf-8"),
        (
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f"Content-Type: {mime_type}\r\n\r\n"
        ).encode("utf-8"),
        file_bytes,
        f"\r\n--{boundary}--\r\n".encode("utf-8"),
    ]
    body = b"".join(parts)

    url = f"{BASE_URL}/v1/agents/{AGENT_CODE}/uploads"
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
    )
    with urllib.request.urlopen(req) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return payload["data"]  # {"upload_id": ..., "session_id": ..., "type": ..., "mime": ..., "size": ...}


def run_with_attachment(user_id, session_id, upload_id, upload_type, input_text):
    url = f"{BASE_URL}/v1/agents/{AGENT_CODE}/runs"
    body = json.dumps(
        {
            "user_id": user_id,
            "session_id": session_id,
            "input": input_text,
            "mode": "queue",
            "files": [
                {"type": upload_type, "transfer_method": "local_file", "upload_id": upload_id}
            ],
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req) as response:
        return json.loads(response.read().decode("utf-8"))


if __name__ == "__main__":
    upload = upload_file("u-123", "report.pdf")
    result = run_with_attachment(
        "u-123",
        upload["session_id"],
        upload["upload_id"],
        upload["type"],
        "帮我看看这份文件",
    )
    print(result)
```

:::

## 8.4 续接会话

把上一次响应拿到的 `session_id` 传回下一次请求的 body,就是同一段会话的下一轮;不传就是另开一段新会话。`stream` 模式下这个 id 在响应头 `X-Expert-Work-Session-Id` 里,不在响应体。

::: code-group

```bash [curl]
# 第一轮:不传 session_id,开一段新会话;-D - 把响应头也打到 stdout
curl -N -D - -X POST "https://<your-domain>/v1/agents/{agent_code}/runs" \
  -H "Authorization: Bearer ${EXPERT_WORK_API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "u-123",
    "input": "你好,帮我查一下上个月的订单",
    "mode": "stream"
  }'
# → 响应头 X-Expert-Work-Session-Id 就是这段会话的 session_id,记下来

# 第二轮:把上一步拿到的 session_id 传回去,续接同一段对话
curl -N -X POST "https://<your-domain>/v1/agents/{agent_code}/runs" \
  -H "Authorization: Bearer ${EXPERT_WORK_API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "u-123",
    "session_id": "<上一步 X-Expert-Work-Session-Id 响应头的值>",
    "input": "那这个月呢?",
    "mode": "stream"
  }'
```

```python [Python]
import json
import os
import urllib.parse
import urllib.request

API_KEY = os.environ["EXPERT_WORK_API_KEY"]
BASE_URL = "https://<your-domain>"
AGENT_CODE = "{agent_code}"  # 替换成你的 agent_code


def run_stream_and_wait(user_id, input_text, session_id=None):
    """
    发起一次 stream 模式的 run,读到 end 帧为止,返回这次绑定/续接到的 session_id。
    这里只关心"什么时候结束",完整的 SSE 分帧/重连处理见 8.1、8.5。
    """
    url = f"{BASE_URL}/v1/agents/{AGENT_CODE}/runs"
    body = {"user_id": user_id, "input": input_text, "mode": "stream"}
    if session_id:
        body["session_id"] = session_id  # 传了就续接这段会话,不传就开一段新会话

    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
        },
    )

    with urllib.request.urlopen(req) as response:
        new_session_id = response.headers.get("X-Expert-Work-Session-Id")
        buffer = ""
        while True:
            chunk = response.read(1024)
            if not chunk:
                break
            buffer += chunk.decode("utf-8")
            while "\n\n" in buffer:
                raw_frame, buffer = buffer.split("\n\n", 1)
                if "event: end" in raw_frame:
                    return new_session_id

    return new_session_id


def fetch_last_final_answer(user_id, session_id):
    query = urllib.parse.urlencode({"user_id": user_id})
    url = f"{BASE_URL}/v1/agents/{AGENT_CODE}/sessions/{session_id}/messages?{query}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {API_KEY}"})
    with urllib.request.urlopen(req) as response:
        payload = json.loads(response.read().decode("utf-8"))
    for message in reversed(payload["data"]["messages"]):
        if message["role"] == "assistant" and message["channel"] == "final":
            return message["content"]
    return None


if __name__ == "__main__":
    session_id = run_stream_and_wait("u-123", "你好,帮我查一下上个月的订单")
    print("第一轮:", fetch_last_final_answer("u-123", session_id))

    # 把上一轮拿到的 session_id 传回去,续接同一段会话
    session_id = run_stream_and_wait("u-123", "那这个月呢?", session_id=session_id)
    print("第二轮:", fetch_last_final_answer("u-123", session_id))
```

:::

## 8.5 断线重连(带 since_seq)

连接断了不要重新调 `POST .../runs`(那会开一轮新 run)——改用 `GET /v1/agents/{agent_code}/runs/{run_id}/events`,带上"见过的最大 seq"当 `since_seq` 重新接上。`truncated` 帧也走同一条重连路径:把它给的 `next_seq` 直接当下一次的 `since_seq`。

::: code-group

```bash [curl]
# 假设上一条连接处理到 seq=41(见过的最大值)就断了,重连时带上它:
curl -N "https://<your-domain>/v1/agents/{agent_code}/runs/{run_id}/events?user_id=u-123&since_seq=41" \
  -H "Authorization: Bearer ${EXPERT_WORK_API_KEY}"
# 不带 since_seq 会从第 0 帧起重发整个 run,断线重连务必带上这个参数
```

```python [Python]
import json
import os
import socket
import urllib.error
import urllib.parse
import urllib.request

API_KEY = os.environ["EXPERT_WORK_API_KEY"]
BASE_URL = "https://<your-domain>"
AGENT_CODE = "{agent_code}"  # 替换成你的 agent_code
READ_TIMEOUT_S = 30  # 自己设的读超时,不能用默认的"无限等"


def iter_sse_frames(response):
    buffer = ""
    while True:
        chunk = response.read(1024)
        if not chunk:
            return
        buffer += chunk.decode("utf-8")
        while "\n\n" in buffer:
            raw_frame, buffer = buffer.split("\n\n", 1)
            yield raw_frame


def parse_frame(raw_frame):
    event, seq = None, None
    data_lines = []
    for line in raw_frame.split("\n"):
        if line.startswith(":"):
            continue  # 跳过心跳注释行
        if line.startswith("event:"):
            event = line[len("event:"):].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:"):].strip())
        elif line.startswith("id:"):
            seq = int(line[len("id:"):].strip().rsplit("-", 1)[-1])
    data = json.loads("\n".join(data_lines)) if data_lines else None
    return event, data, seq


def consume_one_connection(response, cursor):
    """
    消费一条已建立的连接,返回 (done, new_cursor)。
    done=True 表示收到了 end,整个 run 已经结束。
    done=False 表示这条连接被 truncated 或读超时打断,要用 new_cursor 重连。
    """
    for raw_frame in iter_sse_frames(response):
        if not raw_frame.strip():
            continue
        event, data, seq = parse_frame(raw_frame)
        if seq is not None:
            cursor = seq if cursor is None else max(cursor, seq)  # 见过的最大 seq,不是最后一帧的 seq

        if event == "end":
            print("run 结束,status =", data["status"])
            return True, cursor
        if event == "truncated":
            print("这一页到此为止(未结束),next_seq =", data["next_seq"])
            return False, data["next_seq"]  # 直接拿 next_seq 当下一次 since_seq
        if event is not None:
            print(event, data)

    return False, cursor  # 连接被关闭但没收到 end(比如读超时),外层用当前 cursor 重连


def consume_with_reconnect(user_id, run_id):
    cursor = None  # None = 还没见过任何一帧,首次连接不带 since_seq
    done = False

    while not done:
        params = {"user_id": user_id}
        if cursor is not None:
            params["since_seq"] = cursor
        query = urllib.parse.urlencode(params)
        url = f"{BASE_URL}/v1/agents/{AGENT_CODE}/runs/{run_id}/events?{query}"

        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {API_KEY}"})
        try:
            with urllib.request.urlopen(req, timeout=READ_TIMEOUT_S) as response:
                done, cursor = consume_one_connection(response, cursor)
        except (urllib.error.URLError, socket.timeout):
            # 读超时或连接中断——不重新调 /runs,带着当前 cursor 原样重连
            continue


if __name__ == "__main__":
    consume_with_reconnect("u-123", "<要重连的 run_id>")
```

:::

## 8.6 取消 run

`user_id` 在**请求体**里,不是 query——归属校验用它确认这是发起这次 run 的那个终端用户。取消是幂等的,重复调用不会报错。

::: code-group

```bash [curl]
curl -X POST "https://<your-domain>/v1/agents/{agent_code}/runs/{run_id}:cancel" \
  -H "Authorization: Bearer ${EXPERT_WORK_API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{"user_id": "u-123"}'
```

```python [Python]
import json
import os
import urllib.error
import urllib.request

API_KEY = os.environ["EXPERT_WORK_API_KEY"]
BASE_URL = "https://<your-domain>"
AGENT_CODE = "{agent_code}"  # 替换成你的 agent_code


def cancel_run(user_id, run_id):
    url = f"{BASE_URL}/v1/agents/{AGENT_CODE}/runs/{run_id}:cancel"
    body = json.dumps({"user_id": user_id}).encode("utf-8")  # user_id 在请求体,不是 query
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # 大多数失败响应是 {"success": false, "data": null, "error": {"code": ..., "message": ...}}
        # 但 scope 不足的 403 是裸 {"detail": ...}——读不到 error.code,完整对照表见错误码总表
        error_body = json.loads(exc.read().decode("utf-8"))
        print("取消失败:", exc.code, error_body)
        raise


if __name__ == "__main__":
    result = cancel_run("u-123", "<要取消的 run_id>")
    print(result)  # {"success": true, "data": {"run_id": "...", "stopped": true}, "error": null}
```

:::

## 8.7 审批决策

`user_id` 同样在**请求体**里。下面给 `approve` 与 `reject` 两个例子;`decision: "modify"` 时必须带 `modified_args`,另外两种 `decision` 下禁止传它。

::: code-group

```bash [curl]
# 同意
curl -X POST "https://<your-domain>/v1/agents/{agent_code}/runs/{run_id}:decide" \
  -H "Authorization: Bearer ${EXPERT_WORK_API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "u-123",
    "decision": "approve",
    "mode": "queue"
  }'

# 拒绝(reason 可选)
curl -X POST "https://<your-domain>/v1/agents/{agent_code}/runs/{run_id}:decide" \
  -H "Authorization: Bearer ${EXPERT_WORK_API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "u-123",
    "decision": "reject",
    "reason": "超出预算范围",
    "mode": "queue"
  }'
```

```python [Python]
import json
import os
import urllib.request

API_KEY = os.environ["EXPERT_WORK_API_KEY"]
BASE_URL = "https://<your-domain>"
AGENT_CODE = "{agent_code}"  # 替换成你的 agent_code


def decide_run(user_id, run_id, decision, modified_args=None, reason=None):
    url = f"{BASE_URL}/v1/agents/{AGENT_CODE}/runs/{run_id}:decide"
    body = {"user_id": user_id, "decision": decision, "mode": "queue"}
    if decision == "modify":
        body["modified_args"] = modified_args or {}  # 仅 modify 时必填,其余两种 decision 下禁止传
    if reason is not None:
        body["reason"] = reason

    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req) as response:
        payload = json.loads(response.read().decode("utf-8"))
        new_run_id = response.headers.get("X-Expert-Work-Run-Id")  # 续跑用的新 run_id,不是路径里那个
    return payload, new_run_id


if __name__ == "__main__":
    payload, new_run_id = decide_run("u-123", "<待审批的 run_id>", "approve")
    print("续跑的新 run_id:", new_run_id)
    print(payload)

    payload, new_run_id = decide_run(
        "u-123", "<另一个待审批的 run_id>", "reject", reason="超出预算范围"
    )
    print(payload)
```

:::
