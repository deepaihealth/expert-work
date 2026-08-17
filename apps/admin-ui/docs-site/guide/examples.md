# 10 多语言示例

本章给七个常见场景各配一份完整可跑的最小示例,每个场景给 curl / Python / Node.js / Java 四种语言。几条通用约定:

- **key 一律从环境变量读,示例代码里绝不出现明文 key。** 运行前自己设好:

  ```bash [环境变量]
  export EXPERT_WORK_API_KEY="aforge_pat_..."
  ```

  key 长什么样、怎么申请,见 [认证](./auth)。
- **域名统一写成 `https://<your-domain>`**,替换成你实际对接的地址,见 [通用约定](./conventions) 的「环境地址」。
- `{agent_code}` / `{run_id}` / `{session_id}` 这类花括号占位符,替换成你自己的真实值。
- 请求 / 响应字段含义见 [2 跟 Agent 对话](./chat)、[4 对话过程中的控制](./run-control) 与 [5 查询与管理](./query);SSE 事件含义见 [3 读懂 SSE 流](./sse-events)。

## 10.1 发起 run(stream)并解析 SSE

发一次 `mode: "stream"` 的 run,响应体本身就是 SSE 流。第三方最容易在这一步踩坑的是 SSE 事件拆分——下面示例的注释里标出了必须处理对的四件事。`metadata` / `updates` / `approval` / `retry` / `error` 这几类事件,示例里直接原样打印 `data`,没有再展开解析——具体字段含义见 [SSE 事件格式](./sse-events)。

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
    按行读(response.readline()),攒到一个空行才 yield 整条事件——①一条事件可能有
    id / event / data 好几行,必须攒够一整条事件(遇到空行)才能处理,逐行单独处理会把
    一条事件拆散。

    这里特意用 response.readline() 而不是 response.read(1024) 这种按固定字节数读的
    写法——`response` 是 chunked 传输编码,Python 的 http.client 为了凑够调用方要求
    的字节数,会在当前已到手的数据不够时反复等下一个 HTTP chunk;这条连接一旦读超时,
    已经到手但不满请求字节数的数据会被整个丢弃。哪怕不超时,也会表现成"stream 模式
    不流式、界面攒够一批才一次性出字、看起来卡住"——容易被第三方误判成平台的问题。
    readline() 天然按需向底层多次取数据直到凑出一整行,一行(遇到 SSE 事件内单个字段的
    行尾 "\n")一到就返回,不会有这个"为了凑够定长反而卡住"的毛病。
    """
    lines = []
    while True:
        raw_line = response.readline()
        if not raw_line:
            return  # 连接关闭
        line = raw_line.decode("utf-8").rstrip("\n")
        if line == "":
            if lines:
                yield "\n".join(lines)
                lines = []
            continue
        lines.append(line)


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

    max_seq_seen = None  # 维护"见过的最大 seq",断线重连时当游标用(完整重连示例见 10.5)

    with urllib.request.urlopen(req) as response:
        run_id = response.headers.get("X-Expert-Work-Run-Id")

        for raw_frame in iter_sse_frames(response):
            if not raw_frame.strip():
                continue  # 心跳可能在缓冲区里留下一段空白,跳过
            event, data, seq = parse_frame(raw_frame)
            if seq is not None:
                max_seq_seen = seq if max_seq_seen is None else max(max_seq_seen, seq)

            if event == "end":
                # ④收到 end 才算结束;end 事件带这次 run 的终局 status
                print("run 结束,status =", data["status"])
                break
            elif event == "truncated":
                # ④收到 truncated 不算结束——这一页装不下,要带 next_seq 继续拉(见 10.5)
                print("这一页被截断,next_seq =", data["next_seq"])
                continue
            elif event is not None:
                # metadata / updates / approval / retry / error,以及未来可能新增的类型
                print(event, data)

    return run_id


if __name__ == "__main__":
    run_and_stream("u-123", "你好")
```

```js [Node.js]
const API_KEY = process.env.EXPERT_WORK_API_KEY;
const BASE_URL = "https://<your-domain>";
const AGENT_CODE = "{agent_code}"; // 替换成你的 agent_code

async function* iterSseFrames(body) {
  // 从响应流里边读边攒 buffer,按空行("\n\n")拆分事件——①不是按行拆分,也不是按 chunk 拆分。
  // 网络分片不会正好落在事件边界上:一个 chunk 可能只送来半条事件,也可能一次送来不止一条事件;
  // 必须用 buffer 接住上次没读完的部分,下次收到新 chunk 时接着拼,只在真正出现 "\n\n" 时才切出一条事件。
  const decoder = new TextDecoder("utf-8");
  let buffer = "";
  for await (const chunk of body) {
    buffer += decoder.decode(chunk, { stream: true });
    let sepIndex;
    while ((sepIndex = buffer.indexOf("\n\n")) !== -1) {
      yield buffer.slice(0, sepIndex);
      buffer = buffer.slice(sepIndex + 2);
    }
  }
}

function parseFrame(rawFrame) {
  let event = null;
  let seq = null;
  const dataLines = [];
  for (const line of rawFrame.split("\n")) {
    if (line.startsWith(":")) {
      continue; // ②以 ":" 开头的是心跳注释行(服务端约 15 秒发一条),不是事件,跳过
    }
    if (line.startsWith("event:")) {
      event = line.slice("event:".length).trim();
    } else if (line.startsWith("data:")) {
      dataLines.push(line.slice("data:".length).trim());
    } else if (line.startsWith("id:")) {
      // ③id 形如 "{毫秒时间戳}-{seq}",按最后一个 "-" 切分取 seq
      const idValue = line.slice("id:".length).trim();
      seq = Number(idValue.slice(idValue.lastIndexOf("-") + 1));
    }
  }
  const data = dataLines.length > 0 ? JSON.parse(dataLines.join("\n")) : null;
  return { event, data, seq };
}

async function runAndStream(userId, inputText) {
  const url = `${BASE_URL}/v1/agents/${AGENT_CODE}/runs`;
  const response = await fetch(url, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${API_KEY}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ user_id: userId, input: inputText, mode: "stream" }),
  });

  if (!response.ok) {
    // 不查这个会静默失败:错误响应体是普通 JSON(没有 "\n\n"),事件拆分循环直接收不到东西就结束,
    // 退出码 0、零输出——把 agent_code 敲错或者 key 缺 scope 这种情况会看起来"什么都没发生"。
    const body = await response.text();
    throw new Error(`创建 run 失败:${response.status} ${body}`);
  }

  const runId = response.headers.get("X-Expert-Work-Run-Id");
  let maxSeqSeen = null; // 维护"见过的最大 seq",断线重连时当游标用(完整重连示例见 10.5)

  for await (const rawFrame of iterSseFrames(response.body)) {
    if (!rawFrame.trim()) {
      continue; // 心跳可能在缓冲区里留下一段空白,跳过
    }
    const { event, data, seq } = parseFrame(rawFrame);
    if (seq !== null) {
      maxSeqSeen = maxSeqSeen === null ? seq : Math.max(maxSeqSeen, seq);
    }

    if (event === "end") {
      // ④收到 end 才算结束;end 事件带这次 run 的终局 status
      console.log("run 结束,status =", data.status);
      break;
    } else if (event === "truncated") {
      // ④收到 truncated 不算结束——这一页装不下,要带 next_seq 继续拉(见 10.5)
      console.log("这一页被截断,next_seq =", data.next_seq);
    } else if (event !== null) {
      // metadata / updates / approval / retry / error,以及未来可能新增的类型
      console.log(event, data);
    }
  }

  return runId;
}

async function main() {
  await runAndStream("u-123", "你好");
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
```

```java [Java]
import java.io.IOException;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.io.Reader;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;

/**
 * 10.1 发起 run(stream)并解析 SSE —— JDK 8 + HttpURLConnection,零依赖。
 * JSON 用手拼字符串构造请求体,生产代码请用 Gson / Jackson 这类正经 JSON 库,别手拼。
 */
public class RunAndStream {

    static final String API_KEY = System.getenv("EXPERT_WORK_API_KEY");
    static final String BASE_URL = "https://<your-domain>";
    static final String AGENT_CODE = "{agent_code}"; // 替换成你的 agent_code

    /** 一条事件的内容:event 名 + 原始 data JSON 文本(未解析) + seq。 */
    static class Frame {
        String event;
        String rawData;
        Long seq;
    }

    static Frame parseFrame(String rawFrame) {
        Frame frame = new Frame();
        StringBuilder dataBuilder = new StringBuilder();
        boolean hasData = false;
        for (String line : rawFrame.split("\n", -1)) {
            if (line.startsWith(":")) {
                continue; // ②以 ":" 开头的是心跳注释行(服务端约 15 秒发一条),不是事件,跳过
            }
            if (line.startsWith("event:")) {
                frame.event = line.substring("event:".length()).trim();
            } else if (line.startsWith("data:")) {
                if (hasData) {
                    dataBuilder.append("\n");
                }
                dataBuilder.append(line.substring("data:".length()).trim());
                hasData = true;
            } else if (line.startsWith("id:")) {
                // ③id 形如 "{毫秒时间戳}-{seq}",按最后一个 "-" 切分取 seq
                String idValue = line.substring("id:".length()).trim();
                frame.seq = Long.parseLong(idValue.substring(idValue.lastIndexOf('-') + 1));
            }
        }
        frame.rawData = hasData ? dataBuilder.toString() : null;
        return frame;
    }

    // 极简 JSON 取值——从 JSON 文本里找到 "key": 后面的值:对象/数组保留原始文本({...} / [...]),
    // 字符串去掉外层引号并处理转义,数字/true/false/null 原样返回。只扫描一层,不是通用 JSON
    // parser。生产代码请用 Gson / Jackson 这类正经 JSON 库,这里为了保持示例零依赖才手写。
    static String jsonValue(String json, String key) {
        if (json == null) {
            return null;
        }
        String needle = "\"" + key + "\"";
        int keyIdx = json.indexOf(needle);
        if (keyIdx < 0) {
            return null;
        }
        int colonIdx = json.indexOf(':', keyIdx + needle.length());
        int i = colonIdx + 1;
        while (i < json.length() && Character.isWhitespace(json.charAt(i))) {
            i++;
        }
        if (i >= json.length()) {
            return null;
        }
        char c = json.charAt(i);
        if (c == '{' || c == '[') {
            char open = c;
            char close = (c == '{') ? '}' : ']';
            int depth = 0;
            boolean inString = false;
            int start = i;
            for (; i < json.length(); i++) {
                char ch = json.charAt(i);
                if (inString) {
                    if (ch == '\\') {
                        i++;
                    } else if (ch == '"') {
                        inString = false;
                    }
                    continue;
                }
                if (ch == '"') {
                    inString = true;
                } else if (ch == open) {
                    depth++;
                } else if (ch == close) {
                    depth--;
                    if (depth == 0) {
                        i++;
                        break;
                    }
                }
            }
            return json.substring(start, i);
        } else if (c == '"') {
            int start = i + 1;
            StringBuilder sb = new StringBuilder();
            int j = start;
            while (json.charAt(j) != '"') {
                if (json.charAt(j) == '\\') {
                    sb.append(json.charAt(j + 1));
                    j += 2;
                } else {
                    sb.append(json.charAt(j));
                    j++;
                }
            }
            return sb.toString();
        } else {
            int start = i;
            while (i < json.length() && ",}] \t\n\r".indexOf(json.charAt(i)) < 0) {
                i++;
            }
            return json.substring(start, i);
        }
    }

    // JSON 字符串转义——手拼 JSON 时,插值进去的字符串必须转义,不然输入里出现一个双引号
    // (比如 input 是 他说"你好")就会把请求体拼坏,服务端解析失败(422),不是风格建议。
    // 生产代码请用 Gson / Jackson 这类正经 JSON 库自动处理,这里手写是为了保持示例零依赖。
    static String jsonEscape(String s) {
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            if (c == '"' || c == '\\') {
                sb.append('\\').append(c);
            } else if (c == '\n') {
                sb.append("\\n");
            } else if (c == '\r') {
                sb.append("\\r");
            } else if (c == '\t') {
                sb.append("\\t");
            } else if (c < 0x20) {
                sb.append(String.format("\\u%04x", (int) c));
            } else {
                sb.append(c);
            }
        }
        return sb.toString();
    }

    /**
     * 非 2xx 时真正的原因在 getErrorStream() 里,getInputStream() 只会抛一句干巴巴的状态码。
     * 必须判 null:错误响应没有 body(网关的空体 502 就是这样)时 getErrorStream() 返回
     * null,直接拿去读会当场 NPE,连状态码都跟着一起丢掉。
     */
    static String readErrorBody(HttpURLConnection connection) throws IOException {
        InputStream err = connection.getErrorStream();
        if (err == null) {
            return "";
        }
        try (Reader reader = new InputStreamReader(err, StandardCharsets.UTF_8)) {
            StringBuilder sb = new StringBuilder();
            char[] buf = new char[512];
            int n;
            while ((n = reader.read(buf)) != -1) {
                sb.append(buf, 0, n);
            }
            return sb.toString();
        }
    }

    static String runAndStream(String userId, String inputText) throws IOException {
        URL url = new URL(BASE_URL + "/v1/agents/" + AGENT_CODE + "/runs");
        HttpURLConnection connection = (HttpURLConnection) url.openConnection();
        connection.setRequestMethod("POST");
        connection.setRequestProperty("Authorization", "Bearer " + API_KEY);
        connection.setRequestProperty("Content-Type", "application/json");
        connection.setDoOutput(true);

        String body = "{"
                + "\"user_id\":\"" + jsonEscape(userId) + "\","
                + "\"input\":\"" + jsonEscape(inputText) + "\","
                + "\"mode\":\"stream\""
                + "}";
        try (OutputStream out = connection.getOutputStream()) {
            out.write(body.getBytes(StandardCharsets.UTF_8));
        }

        try {
            int status = connection.getResponseCode();
            if (status < 200 || status >= 300) {
                // 直接 getInputStream() 只会抛一句 "Server returned HTTP response code: 4xx",
                // 到底是 agent_code 敲错了还是 key 缺 scope,得从 getErrorStream() 里读响应体
                // 才看得到——不显式查状态码就永远读不到它。
                throw new IOException("创建 run 失败:" + status + " " + readErrorBody(connection));
            }

            String runId = connection.getHeaderField("X-Expert-Work-Run-Id");
            Long maxSeqSeen = null; // 维护"见过的最大 seq",断线重连时当游标用(完整重连示例见 10.5)

            try (InputStream in = connection.getInputStream()) {
                // InputStreamReader 必须显式指定 UTF-8——JDK 8 的默认字符集跟平台走,不指定在中文环境下会乱码
                Reader reader = new InputStreamReader(in, StandardCharsets.UTF_8);
                char[] chunk = new char[1024];
                StringBuilder buffer = new StringBuilder();
                int n;
                readLoop:
                while ((n = reader.read(chunk)) != -1) {
                    buffer.append(chunk, 0, n);
                    int sep;
                    while ((sep = buffer.indexOf("\n\n")) != -1) {
                        String rawFrame = buffer.substring(0, sep);
                        buffer.delete(0, sep + 2);
                        if (rawFrame.trim().isEmpty()) {
                            continue; // 心跳可能在缓冲区里留下一段空白,跳过
                        }
                        Frame frame = parseFrame(rawFrame);
                        if (frame.seq != null) {
                            maxSeqSeen = (maxSeqSeen == null) ? frame.seq : Math.max(maxSeqSeen, frame.seq);
                        }
                        if ("end".equals(frame.event)) {
                            // ④收到 end 才算结束;end 事件带这次 run 的终局 status
                            System.out.println("run 结束,status = " + jsonValue(frame.rawData, "status"));
                            break readLoop;
                        } else if ("truncated".equals(frame.event)) {
                            // ④收到 truncated 不算结束——这一页装不下,要带 next_seq 继续拉(见 10.5)
                            System.out.println("这一页被截断,next_seq = " + jsonValue(frame.rawData, "next_seq"));
                        } else if (frame.event != null) {
                            // metadata / updates / approval / retry / error,以及未来可能新增的类型
                            System.out.println(frame.event + " " + frame.rawData);
                        }
                    }
                }
            }

            return runId;
        } finally {
            connection.disconnect();
        }
    }

    public static void main(String[] args) throws IOException {
        runAndStream("u-123", "你好");
    }
}
```

:::

## 10.2 queue 模式 + 轮询结果

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

```js [Node.js]
const API_KEY = process.env.EXPERT_WORK_API_KEY;
const BASE_URL = "https://<your-domain>";
const AGENT_CODE = "{agent_code}"; // 替换成你的 agent_code

async function getJson(url) {
  const response = await fetch(url, {
    headers: { Authorization: `Bearer ${API_KEY}` },
  });
  if (!response.ok) {
    throw new Error(`请求失败:${response.status} ${await response.text()}`);
  }
  return response.json();
}

async function startQueueRun(userId, inputText) {
  const url = `${BASE_URL}/v1/agents/${AGENT_CODE}/runs`;
  const response = await fetch(url, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${API_KEY}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ user_id: userId, input: inputText, mode: "queue" }),
  });
  if (!response.ok) {
    throw new Error(`创建 run 失败:${response.status} ${await response.text()}`);
  }
  const payload = await response.json();
  return payload.data; // {"run_id": "...", "thread_id": "...", "status": "queued"}
}

async function isSessionRunning(userId, sessionId) {
  const query = new URLSearchParams({ user_id: userId });
  const url = `${BASE_URL}/v1/agents/${AGENT_CODE}/sessions?${query}`;
  const payload = await getJson(url);
  for (const session of payload.data.sessions) {
    if (session.session_id === sessionId) {
      return session.running;
    }
  }
  return false; // 查不到这个 session 时按未在跑处理
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function pollUntilDone(userId, sessionId, intervalMs = 2000, maxAttempts = 30) {
  for (let i = 0; i < maxAttempts; i++) {
    if (!(await isSessionRunning(userId, sessionId))) {
      return;
    }
    await sleep(intervalMs);
  }
  throw new Error("超过最大轮询次数,run 仍未结束");
}

async function fetchLastFinalAnswer(userId, sessionId) {
  const query = new URLSearchParams({ user_id: userId });
  const url = `${BASE_URL}/v1/agents/${AGENT_CODE}/sessions/${sessionId}/messages?${query}`;
  const payload = await getJson(url);
  const messages = payload.data.messages;
  for (let i = messages.length - 1; i >= 0; i--) {
    const message = messages[i];
    if (message.role === "assistant" && message.channel === "final") {
      return message.content;
    }
  }
  return null;
}

async function main() {
  const result = await startQueueRun("u-123", "帮我整理一份周报");
  const sessionId = result.thread_id; // queue 响应里字段名是 thread_id,续接时仍传给 session_id
  await pollUntilDone("u-123", sessionId);
  console.log(await fetchLastFinalAnswer("u-123", sessionId));
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
```

```java [Java]
import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.io.UnsupportedEncodingException;
import java.net.HttpURLConnection;
import java.net.URL;
import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;

/**
 * 10.2 queue 模式 + 轮询结果 —— JDK 8 + HttpURLConnection,零依赖。
 * JSON 用手拼字符串构造请求体,响应用下面的极简取值函数抠字段,生产代码请用 Gson / Jackson
 * 这类正经 JSON 库,别手拼、也别自己写 parser。
 */
public class QueueAndPoll {

    static final String API_KEY = System.getenv("EXPERT_WORK_API_KEY");
    static final String BASE_URL = "https://<your-domain>";
    static final String AGENT_CODE = "{agent_code}"; // 替换成你的 agent_code

    static String readBody(InputStream in) throws IOException {
        // InputStreamReader 必须显式指定 UTF-8——JDK 8 默认字符集跟平台走,中文会乱码
        try (BufferedReader reader = new BufferedReader(new InputStreamReader(in, StandardCharsets.UTF_8))) {
            StringBuilder sb = new StringBuilder();
            String line;
            while ((line = reader.readLine()) != null) {
                sb.append(line);
            }
            return sb.toString();
        }
    }

    static String urlEncode(String value) {
        try {
            return URLEncoder.encode(value, "UTF-8");
        } catch (UnsupportedEncodingException e) {
            throw new RuntimeException(e); // UTF-8 恒受支持,不会真的走到这里
        }
    }

    // 极简 JSON 取值——从 JSON 文本里找到 "key": 后面的值:对象/数组保留原始文本({...} / [...]),
    // 字符串去掉外层引号并处理转义,数字/true/false/null 原样返回。只扫描一层,不是通用 JSON
    // parser。生产代码请用 Gson / Jackson 这类正经 JSON 库,这里为了保持示例零依赖才手写。
    static String jsonValue(String json, String key) {
        if (json == null) {
            return null;
        }
        String needle = "\"" + key + "\"";
        int keyIdx = json.indexOf(needle);
        if (keyIdx < 0) {
            return null;
        }
        int colonIdx = json.indexOf(':', keyIdx + needle.length());
        int i = colonIdx + 1;
        while (i < json.length() && Character.isWhitespace(json.charAt(i))) {
            i++;
        }
        if (i >= json.length()) {
            return null;
        }
        char c = json.charAt(i);
        if (c == '{' || c == '[') {
            char open = c;
            char close = (c == '{') ? '}' : ']';
            int depth = 0;
            boolean inString = false;
            int start = i;
            for (; i < json.length(); i++) {
                char ch = json.charAt(i);
                if (inString) {
                    if (ch == '\\') {
                        i++;
                    } else if (ch == '"') {
                        inString = false;
                    }
                    continue;
                }
                if (ch == '"') {
                    inString = true;
                } else if (ch == open) {
                    depth++;
                } else if (ch == close) {
                    depth--;
                    if (depth == 0) {
                        i++;
                        break;
                    }
                }
            }
            return json.substring(start, i);
        } else if (c == '"') {
            int start = i + 1;
            StringBuilder sb = new StringBuilder();
            int j = start;
            while (json.charAt(j) != '"') {
                if (json.charAt(j) == '\\') {
                    sb.append(json.charAt(j + 1));
                    j += 2;
                } else {
                    sb.append(json.charAt(j));
                    j++;
                }
            }
            return sb.toString();
        } else {
            int start = i;
            while (i < json.length() && ",}] \t\n\r".indexOf(json.charAt(i)) < 0) {
                i++;
            }
            return json.substring(start, i);
        }
    }

    // 把一个顶层 JSON 数组的原始文本("[{...}, {...}]")按大括号配对拆成每个元素的原始文本,
    // 同样只扫描一层,不是通用 JSON parser。
    static List<String> splitJsonArray(String arrayText) {
        List<String> items = new ArrayList<>();
        if (arrayText == null) {
            return items;
        }
        int depth = 0;
        boolean inString = false;
        int start = -1;
        for (int i = 0; i < arrayText.length(); i++) {
            char ch = arrayText.charAt(i);
            if (inString) {
                if (ch == '\\') {
                    i++;
                } else if (ch == '"') {
                    inString = false;
                }
                continue;
            }
            if (ch == '"') {
                inString = true;
            } else if (ch == '{') {
                if (depth == 0) {
                    start = i;
                }
                depth++;
            } else if (ch == '}') {
                depth--;
                if (depth == 0) {
                    items.add(arrayText.substring(start, i + 1));
                }
            }
        }
        return items;
    }

    // JSON 字符串转义——手拼 JSON 时,插值进去的字符串必须转义,不然输入里出现一个双引号
    // (比如 input 是 他说"你好")就会把请求体拼坏,服务端解析失败(422),不是风格建议。
    // 生产代码请用 Gson / Jackson 这类正经 JSON 库自动处理,这里手写是为了保持示例零依赖。
    static String jsonEscape(String s) {
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            if (c == '"' || c == '\\') {
                sb.append('\\').append(c);
            } else if (c == '\n') {
                sb.append("\\n");
            } else if (c == '\r') {
                sb.append("\\r");
            } else if (c == '\t') {
                sb.append("\\t");
            } else if (c < 0x20) {
                sb.append(String.format("\\u%04x", (int) c));
            } else {
                sb.append(c);
            }
        }
        return sb.toString();
    }

    /**
     * 非 2xx 时真正的原因在 getErrorStream() 里,getInputStream() 只会抛一句干巴巴的状态码。
     * 必须判 null:错误响应没有 body(网关的空体 502 就是这样)时 getErrorStream() 返回
     * null,直接丢给 readBody 会当场 NPE,连状态码都跟着一起丢掉。
     */
    static String readErrorBody(HttpURLConnection connection) throws IOException {
        InputStream err = connection.getErrorStream();
        return (err == null) ? "" : readBody(err);
    }

    static String getJson(String url) throws IOException {
        HttpURLConnection connection = (HttpURLConnection) new URL(url).openConnection();
        connection.setRequestProperty("Authorization", "Bearer " + API_KEY);
        try {
            int status = connection.getResponseCode();
            if (status < 200 || status >= 300) {
                // 先查状态码再读:直接 getInputStream() 只会抛一句
                // "Server returned HTTP response code: 4xx",错误码和原因得从
                // getErrorStream() 里读响应体才看得到。
                throw new IOException("请求失败:" + status + " " + readErrorBody(connection));
            }
            return readBody(connection.getInputStream());
        } finally {
            connection.disconnect();
        }
    }

    static String startQueueRun(String userId, String inputText) throws IOException {
        URL url = new URL(BASE_URL + "/v1/agents/" + AGENT_CODE + "/runs");
        HttpURLConnection connection = (HttpURLConnection) url.openConnection();
        connection.setRequestMethod("POST");
        connection.setRequestProperty("Authorization", "Bearer " + API_KEY);
        connection.setRequestProperty("Content-Type", "application/json");
        connection.setDoOutput(true);

        String body = "{\"user_id\":\"" + jsonEscape(userId) + "\",\"input\":\"" + jsonEscape(inputText)
                + "\",\"mode\":\"queue\"}";
        try (OutputStream out = connection.getOutputStream()) {
            out.write(body.getBytes(StandardCharsets.UTF_8));
        }

        try {
            int status = connection.getResponseCode();
            if (status < 200 || status >= 300) {
                // 同上:非 2xx 的原因只在 getErrorStream() 里,不查状态码就直接读会丢掉它
                throw new IOException("创建 run 失败:" + status + " " + readErrorBody(connection));
            }
            String responseBody = readBody(connection.getInputStream());
            return jsonValue(responseBody, "data"); // {"run_id": "...", "thread_id": "...", "status": "queued"}
        } finally {
            connection.disconnect();
        }
    }

    static boolean isSessionRunning(String userId, String sessionId) throws IOException {
        String query = "user_id=" + urlEncode(userId);
        String url = BASE_URL + "/v1/agents/" + AGENT_CODE + "/sessions?" + query;
        String responseBody = getJson(url);
        String sessionsArray = jsonValue(jsonValue(responseBody, "data"), "sessions");
        for (String session : splitJsonArray(sessionsArray)) {
            if (sessionId.equals(jsonValue(session, "session_id"))) {
                return "true".equals(jsonValue(session, "running"));
            }
        }
        return false; // 查不到这个 session 时按未在跑处理
    }

    static void pollUntilDone(String userId, String sessionId, long intervalMs, int maxAttempts)
            throws IOException, InterruptedException {
        for (int i = 0; i < maxAttempts; i++) {
            if (!isSessionRunning(userId, sessionId)) {
                return;
            }
            Thread.sleep(intervalMs);
        }
        throw new IllegalStateException("超过最大轮询次数,run 仍未结束");
    }

    static String fetchLastFinalAnswer(String userId, String sessionId) throws IOException {
        String query = "user_id=" + urlEncode(userId);
        String url = BASE_URL + "/v1/agents/" + AGENT_CODE + "/sessions/" + sessionId + "/messages?" + query;
        String responseBody = getJson(url);
        String messagesArray = jsonValue(jsonValue(responseBody, "data"), "messages");
        List<String> messages = splitJsonArray(messagesArray);
        for (int i = messages.size() - 1; i >= 0; i--) {
            String message = messages.get(i);
            if ("assistant".equals(jsonValue(message, "role")) && "final".equals(jsonValue(message, "channel"))) {
                return jsonValue(message, "content");
            }
        }
        return null;
    }

    public static void main(String[] args) throws IOException, InterruptedException {
        String result = startQueueRun("u-123", "帮我整理一份周报");
        String sessionId = jsonValue(result, "thread_id"); // queue 响应里字段名是 thread_id,续接时仍传给 session_id
        pollUntilDone("u-123", sessionId, 2000, 30);
        System.out.println(fetchLastFinalAnswer("u-123", sessionId));
    }
}
```

:::

## 10.3 上传文件并带进 run

先调上传接口拿 `upload_id`,再原样放进 `files[]` 发起 run。下面示例传一份文档(`type: "document"`);图片走法一样,唯一区别是 `upload_id` 的格式(`expert_work://image/...`),细节见 [2.6 带图片和文档](./chat#_2-6-带图片和文档) 的「`files[]`」一节。

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
import os
import urllib.request
import uuid

API_KEY = os.environ["EXPERT_WORK_API_KEY"]
BASE_URL = "https://<your-domain>"
AGENT_CODE = "{agent_code}"  # 替换成你的 agent_code

# 只按扩展名猜第 8 章 错误码总表(`INVALID_UPLOAD` 那条)列出的那几种受支持类型,
# 不是通用 MIME 猜测器,内容和下面 Node.js / Java 两份表逐条一致。
# **不要用 mimetypes.guess_type**:它认得哪些类型既跟 Python 版本走,也跟宿主机的 mime
# 配置文件走(标准库启动时会去读 /etc/mime.types 这类系统文件),同一份代码换个环境
# 结果就可能不一样。实测 CPython 3.9 / 3.10 / 3.11 上 `.md` 返回 None、3.12 起才认得;
# None 回退成 application/octet-stream 后不在任何白名单里,上传每次必然 400 INVALID_UPLOAD。
EXTENSION_MIME_TYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".csv": "text/csv",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def guess_mime_type(filename):
    ext = os.path.splitext(filename)[1].lower()
    mime_type = EXTENSION_MIME_TYPES.get(ext)
    if mime_type is None:
        # 不回退到 application/octet-stream:那个值一定会被服务端拒掉,与其发一次注定
        # 400 的请求,不如在本地就报清楚是哪个文件、支持哪些扩展名。
        raise ValueError(
            f"不支持的文件类型:{filename};"
            f"受支持的扩展名:{' '.join(EXTENSION_MIME_TYPES)}"
        )
    return mime_type


def upload_file(user_id, file_path):
    boundary = uuid.uuid4().hex
    filename = os.path.basename(file_path)
    mime_type = guess_mime_type(filename)

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

```js [Node.js]
const fs = require("node:fs");
const path = require("node:path");

const API_KEY = process.env.EXPERT_WORK_API_KEY;
const BASE_URL = "https://<your-domain>";
const AGENT_CODE = "{agent_code}"; // 替换成你的 agent_code

// 只按扩展名猜第 8 章 错误码总表(`INVALID_UPLOAD` 那条)列出的那几种受支持类型,
// 不是通用 MIME 猜测器。new Blob([...]) 不传 type 时,fetch/undici 发出的 Content-Type
// 会是 application/octet-stream——这个类型不在文档类/图片类任何一个白名单里,
// 上传会**每次必然** 400 INVALID_UPLOAD。
const EXTENSION_MIME_TYPES = {
  ".pdf": "application/pdf",
  ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
  ".txt": "text/plain",
  ".md": "text/markdown",
  ".csv": "text/csv",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".webp": "image/webp",
  ".gif": "image/gif",
};

function guessMimeType(filename) {
  const ext = path.extname(filename).toLowerCase();
  const mimeType = EXTENSION_MIME_TYPES[ext];
  if (!mimeType) {
    // 不回退到 application/octet-stream:那个值一定会被服务端拒掉,与其发一次注定
    // 400 的请求,不如在本地就报清楚是哪个文件、支持哪些扩展名。
    throw new Error(
      `不支持的文件类型:${filename};受支持的扩展名:${Object.keys(EXTENSION_MIME_TYPES).join(" ")}`
    );
  }
  return mimeType;
}

async function uploadFile(userId, filePath) {
  // Node 内建 FormData + fetch 会自动生成 multipart/form-data 边界并设置 Content-Type,
  // 不需要像 Python 标准库那样手写 multipart 编码。
  const fileBuffer = fs.readFileSync(filePath);
  const filename = path.basename(filePath);
  const form = new FormData();
  form.append("user_id", userId);
  form.append("file", new Blob([fileBuffer], { type: guessMimeType(filename) }), filename);

  const url = `${BASE_URL}/v1/agents/${AGENT_CODE}/uploads`;
  const response = await fetch(url, {
    method: "POST",
    headers: { Authorization: `Bearer ${API_KEY}` }, // 不要手动设置 Content-Type,fetch 会带上正确的 boundary
    body: form,
  });
  if (!response.ok) {
    throw new Error(`上传失败:${response.status} ${await response.text()}`);
  }
  const payload = await response.json();
  return payload.data; // {"upload_id": ..., "session_id": ..., "type": ..., "mime": ..., "size": ...}
}

async function runWithAttachment(userId, sessionId, uploadId, uploadType, inputText) {
  const url = `${BASE_URL}/v1/agents/${AGENT_CODE}/runs`;
  const response = await fetch(url, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${API_KEY}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      user_id: userId,
      session_id: sessionId,
      input: inputText,
      mode: "queue",
      files: [{ type: uploadType, transfer_method: "local_file", upload_id: uploadId }],
    }),
  });
  if (!response.ok) {
    throw new Error(`创建 run 失败:${response.status} ${await response.text()}`);
  }
  return response.json();
}

async function main() {
  const upload = await uploadFile("u-123", "report.pdf");
  const result = await runWithAttachment(
    "u-123",
    upload.session_id,
    upload.upload_id,
    upload.type,
    "帮我看看这份文件"
  );
  console.log(result);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
```

```java [Java]
import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Paths;
import java.util.LinkedHashMap;
import java.util.Locale;
import java.util.Map;
import java.util.UUID;

/**
 * 10.3 上传文件并带进 run —— JDK 8 + HttpURLConnection,零依赖。
 * multipart/form-data 请求体需要手写(java.net 标准库没有内置 multipart 编码器);
 * JSON 同样手拼字符串构造请求体,生产代码请用 Gson / Jackson 这类正经 JSON 库,别手拼。
 */
public class UploadAndRun {

    static final String API_KEY = System.getenv("EXPERT_WORK_API_KEY");
    static final String BASE_URL = "https://<your-domain>";
    static final String AGENT_CODE = "{agent_code}"; // 替换成你的 agent_code

    // 只按扩展名猜第 8 章 错误码总表(`INVALID_UPLOAD` 那条)列出的那几种受支持类型,
    // 不是通用 MIME 猜测器,内容和上面 Python / Node.js 两份表逐条一致。
    // **不要用 URLConnection.guessContentTypeFromName**:它查的是 JDK 自带的那张老
    // content-types.properties,JDK 8 上 .docx / .xlsx / .pptx / .md / .csv / .webp
    // 六种全部返回 null(只有 .pdf / .txt / .png / .jpg / .jpeg / .gif 能查到),
    // null 回退成 application/octet-stream 后不在任何白名单里,上传**每次必然**
    // 400 INVALID_UPLOAD——11 种受支持类型里有 6 种会因此完全用不了。
    static final Map<String, String> EXTENSION_MIME_TYPES = new LinkedHashMap<String, String>();

    static {
        EXTENSION_MIME_TYPES.put(".pdf", "application/pdf");
        EXTENSION_MIME_TYPES.put(
                ".docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document");
        EXTENSION_MIME_TYPES.put(
                ".xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet");
        EXTENSION_MIME_TYPES.put(
                ".pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation");
        EXTENSION_MIME_TYPES.put(".txt", "text/plain");
        EXTENSION_MIME_TYPES.put(".md", "text/markdown");
        EXTENSION_MIME_TYPES.put(".csv", "text/csv");
        EXTENSION_MIME_TYPES.put(".png", "image/png");
        EXTENSION_MIME_TYPES.put(".jpg", "image/jpeg");
        EXTENSION_MIME_TYPES.put(".jpeg", "image/jpeg");
        EXTENSION_MIME_TYPES.put(".webp", "image/webp");
        EXTENSION_MIME_TYPES.put(".gif", "image/gif");
    }

    static String guessMimeType(String filename) {
        int dotIdx = filename.lastIndexOf('.');
        String ext = (dotIdx < 0) ? "" : filename.substring(dotIdx).toLowerCase(Locale.ROOT);
        String mimeType = EXTENSION_MIME_TYPES.get(ext);
        if (mimeType == null) {
            // 不回退到 application/octet-stream:那个值一定会被服务端拒掉,与其发一次注定
            // 400 的请求,不如在本地就报清楚是哪个文件、支持哪些扩展名。
            throw new IllegalArgumentException("不支持的文件类型:" + filename
                    + ";受支持的扩展名:" + EXTENSION_MIME_TYPES.keySet());
        }
        return mimeType;
    }

    static String readBody(InputStream in) throws IOException {
        // InputStreamReader 必须显式指定 UTF-8——JDK 8 默认字符集跟平台走,中文会乱码
        try (BufferedReader reader = new BufferedReader(new InputStreamReader(in, StandardCharsets.UTF_8))) {
            StringBuilder sb = new StringBuilder();
            String line;
            while ((line = reader.readLine()) != null) {
                sb.append(line);
            }
            return sb.toString();
        }
    }

    /** 非 2xx 时真正的原因在 getErrorStream() 里,getInputStream() 只会抛一句干巴巴的状态码。 */
    static String readErrorBody(HttpURLConnection connection) throws IOException {
        InputStream err = connection.getErrorStream();
        return (err == null) ? "" : readBody(err);
    }

    // 极简 JSON 取值,详细注释见 10.1/10.2 示例;这里只用到抠 "data" 这个嵌套对象。
    static String jsonValue(String json, String key) {
        if (json == null) {
            return null;
        }
        String needle = "\"" + key + "\"";
        int keyIdx = json.indexOf(needle);
        if (keyIdx < 0) {
            return null;
        }
        int colonIdx = json.indexOf(':', keyIdx + needle.length());
        int i = colonIdx + 1;
        while (i < json.length() && Character.isWhitespace(json.charAt(i))) {
            i++;
        }
        if (i >= json.length()) {
            return null;
        }
        char c = json.charAt(i);
        if (c == '{' || c == '[') {
            char open = c;
            char close = (c == '{') ? '}' : ']';
            int depth = 0;
            boolean inString = false;
            int start = i;
            for (; i < json.length(); i++) {
                char ch = json.charAt(i);
                if (inString) {
                    if (ch == '\\') {
                        i++;
                    } else if (ch == '"') {
                        inString = false;
                    }
                    continue;
                }
                if (ch == '"') {
                    inString = true;
                } else if (ch == open) {
                    depth++;
                } else if (ch == close) {
                    depth--;
                    if (depth == 0) {
                        i++;
                        break;
                    }
                }
            }
            return json.substring(start, i);
        } else if (c == '"') {
            int start = i + 1;
            StringBuilder sb = new StringBuilder();
            int j = start;
            while (json.charAt(j) != '"') {
                if (json.charAt(j) == '\\') {
                    sb.append(json.charAt(j + 1));
                    j += 2;
                } else {
                    sb.append(json.charAt(j));
                    j++;
                }
            }
            return sb.toString();
        } else {
            int start = i;
            while (i < json.length() && ",}] \t\n\r".indexOf(json.charAt(i)) < 0) {
                i++;
            }
            return json.substring(start, i);
        }
    }

    // JSON 字符串转义——手拼 JSON 时,插值进去的字符串必须转义,不然输入里出现一个双引号
    // (比如 input 是 他说"你好")就会把请求体拼坏,服务端解析失败(422),不是风格建议。
    // 生产代码请用 Gson / Jackson 这类正经 JSON 库自动处理,这里手写是为了保持示例零依赖。
    static String jsonEscape(String s) {
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            if (c == '"' || c == '\\') {
                sb.append('\\').append(c);
            } else if (c == '\n') {
                sb.append("\\n");
            } else if (c == '\r') {
                sb.append("\\r");
            } else if (c == '\t') {
                sb.append("\\t");
            } else if (c < 0x20) {
                sb.append(String.format("\\u%04x", (int) c));
            } else {
                sb.append(c);
            }
        }
        return sb.toString();
    }

    static String uploadFile(String userId, String filePath) throws IOException {
        String boundary = UUID.randomUUID().toString();
        byte[] fileBytes = Files.readAllBytes(Paths.get(filePath));
        String filename = Paths.get(filePath).getFileName().toString();
        String mimeType = guessMimeType(filename);

        URL url = new URL(BASE_URL + "/v1/agents/" + AGENT_CODE + "/uploads");
        HttpURLConnection connection = (HttpURLConnection) url.openConnection();
        connection.setRequestMethod("POST");
        connection.setRequestProperty("Authorization", "Bearer " + API_KEY);
        connection.setRequestProperty("Content-Type", "multipart/form-data; boundary=" + boundary);
        connection.setDoOutput(true);

        try (OutputStream out = connection.getOutputStream()) {
            out.write(("--" + boundary + "\r\n").getBytes(StandardCharsets.UTF_8));
            out.write("Content-Disposition: form-data; name=\"user_id\"\r\n\r\n".getBytes(StandardCharsets.UTF_8));
            out.write((userId + "\r\n").getBytes(StandardCharsets.UTF_8));

            out.write(("--" + boundary + "\r\n").getBytes(StandardCharsets.UTF_8));
            out.write(("Content-Disposition: form-data; name=\"file\"; filename=\"" + filename + "\"\r\n")
                    .getBytes(StandardCharsets.UTF_8));
            out.write(("Content-Type: " + mimeType + "\r\n\r\n").getBytes(StandardCharsets.UTF_8));
            out.write(fileBytes);
            out.write(("\r\n--" + boundary + "--\r\n").getBytes(StandardCharsets.UTF_8));
        }

        try {
            int status = connection.getResponseCode();
            if (status < 200 || status >= 300) {
                // 直接 getInputStream() 只会抛一句 "Server returned HTTP response code: 400",
                // 到底是 INVALID_UPLOAD 还是别的原因得从 getErrorStream() 里读响应体才看得到。
                throw new IOException("上传失败:" + status + " " + readErrorBody(connection));
            }
            String responseBody = readBody(connection.getInputStream());
            return jsonValue(responseBody, "data"); // {"upload_id": ..., "session_id": ..., "type": ..., "mime": ..., "size": ...}
        } finally {
            connection.disconnect();
        }
    }

    static String runWithAttachment(
            String userId, String sessionId, String uploadId, String uploadType, String inputText)
            throws IOException {
        URL url = new URL(BASE_URL + "/v1/agents/" + AGENT_CODE + "/runs");
        HttpURLConnection connection = (HttpURLConnection) url.openConnection();
        connection.setRequestMethod("POST");
        connection.setRequestProperty("Authorization", "Bearer " + API_KEY);
        connection.setRequestProperty("Content-Type", "application/json");
        connection.setDoOutput(true);

        String body = "{"
                + "\"user_id\":\"" + jsonEscape(userId) + "\","
                + "\"session_id\":\"" + jsonEscape(sessionId) + "\","
                + "\"input\":\"" + jsonEscape(inputText) + "\","
                + "\"mode\":\"queue\","
                + "\"files\":[{\"type\":\"" + jsonEscape(uploadType) + "\",\"transfer_method\":\"local_file\","
                + "\"upload_id\":\"" + jsonEscape(uploadId) + "\"}]"
                + "}";
        try (OutputStream out = connection.getOutputStream()) {
            out.write(body.getBytes(StandardCharsets.UTF_8));
        }

        try {
            int status = connection.getResponseCode();
            if (status < 200 || status >= 300) {
                // 和上面 uploadFile 同一形态:直接 getInputStream() 只会抛一句
                // "Server returned HTTP response code: 4xx",错误码和原因得从
                // getErrorStream() 里读响应体才看得到。
                throw new IOException("创建 run 失败:" + status + " " + readErrorBody(connection));
            }
            return readBody(connection.getInputStream());
        } finally {
            connection.disconnect();
        }
    }

    public static void main(String[] args) throws IOException {
        String upload = uploadFile("u-123", "report.pdf");
        String sessionId = jsonValue(upload, "session_id");
        String uploadId = jsonValue(upload, "upload_id");
        String uploadType = jsonValue(upload, "type");
        String result = runWithAttachment("u-123", sessionId, uploadId, uploadType, "帮我看看这份文件");
        System.out.println(result);
    }
}
```

:::

## 10.4 续接会话

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
    发起一次 stream 模式的 run,读到 end 事件为止,返回这次绑定/续接到的 session_id。
    这里只关心"什么时候结束",完整的 SSE 事件拆分/重连处理见 10.1、10.5。
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
        # 按行读(response.readline()),不要用 response.read(1024) 这种按固定字节数读的
        # 写法——chunked 传输编码下会为了凑够字节数反复等下一个 HTTP chunk,响应会被
        # 攒到凑够一批才处理甚至在读超时时丢数据,完整原因见 10.1 的 iter_sse_frames 注释。
        #
        # 只看 event: 字段的值是不是 "end",不要对整条事件文本做 "event: end" 子串匹配——
        # 服务端会流式吐 token 事件,里面是模型生成的原始文本,如果模型回答里恰好出现
        # "event: end" 这几个字符,子串匹配会被这段文本骗过,提前把"其实没跑完"的
        # session_id 返回给你。
        event = None
        while True:
            raw_line = response.readline()
            if not raw_line:
                break
            line = raw_line.decode("utf-8").rstrip("\n")
            if line == "":
                if event == "end":
                    return new_session_id
                event = None
                continue
            if line.startswith("event:"):
                event = line[len("event:"):].strip()

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

```js [Node.js]
const API_KEY = process.env.EXPERT_WORK_API_KEY;
const BASE_URL = "https://<your-domain>";
const AGENT_CODE = "{agent_code}"; // 替换成你的 agent_code

async function runStreamAndWait(userId, inputText, sessionId) {
  // 发起一次 stream 模式的 run,读到 end 事件为止,返回这次绑定/续接到的 session_id。
  // 这里只关心"什么时候结束",完整的 SSE 事件拆分/重连处理见 10.1、10.5。
  const url = `${BASE_URL}/v1/agents/${AGENT_CODE}/runs`;
  const body = { user_id: userId, input: inputText, mode: "stream" };
  if (sessionId) {
    body.session_id = sessionId; // 传了就续接这段会话,不传就开一段新会话
  }

  const response = await fetch(url, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${API_KEY}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(body),
  });

  if (!response.ok) {
    throw new Error(`创建 run 失败:${response.status} ${await response.text()}`);
  }

  const newSessionId = response.headers.get("X-Expert-Work-Session-Id");
  const decoder = new TextDecoder("utf-8");
  let buffer = "";
  // 只看 event: 字段的值是不是 "end",不要对整条事件文本做 "event: end" 子串匹配——服务端会
  // 流式吐 token 事件,里面是模型生成的原始文本,如果模型回答里恰好出现 "event: end" 这几个
  // 字符,子串匹配会被这段文本骗过,提前把"其实没跑完"的 session_id 返回给你。
  for await (const chunk of response.body) {
    buffer += decoder.decode(chunk, { stream: true });
    let sepIndex;
    while ((sepIndex = buffer.indexOf("\n\n")) !== -1) {
      const rawFrame = buffer.slice(0, sepIndex);
      buffer = buffer.slice(sepIndex + 2);
      let event = null;
      for (const line of rawFrame.split("\n")) {
        if (line.startsWith("event:")) {
          event = line.slice("event:".length).trim();
        }
      }
      if (event === "end") {
        return newSessionId;
      }
    }
  }

  return newSessionId;
}

async function fetchLastFinalAnswer(userId, sessionId) {
  const query = new URLSearchParams({ user_id: userId });
  const url = `${BASE_URL}/v1/agents/${AGENT_CODE}/sessions/${sessionId}/messages?${query}`;
  const response = await fetch(url, {
    headers: { Authorization: `Bearer ${API_KEY}` },
  });
  if (!response.ok) {
    // 不查状态码就直接读 data.messages,404 时 data 是 null,报出来的是
    // "Cannot read properties of null",看不出真正发生了什么。
    throw new Error(`拉取历史消息失败:${response.status} ${await response.text()}`);
  }
  const payload = await response.json();
  const messages = payload.data.messages;
  for (let i = messages.length - 1; i >= 0; i--) {
    const message = messages[i];
    if (message.role === "assistant" && message.channel === "final") {
      return message.content;
    }
  }
  return null;
}

async function main() {
  let sessionId = await runStreamAndWait("u-123", "你好,帮我查一下上个月的订单");
  console.log("第一轮:", await fetchLastFinalAnswer("u-123", sessionId));

  // 把上一轮拿到的 session_id 传回去,续接同一段会话
  sessionId = await runStreamAndWait("u-123", "那这个月呢?", sessionId);
  console.log("第二轮:", await fetchLastFinalAnswer("u-123", sessionId));
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
```

```java [Java]
import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.io.Reader;
import java.io.UnsupportedEncodingException;
import java.net.HttpURLConnection;
import java.net.URL;
import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;

/**
 * 10.4 续接会话 —— JDK 8 + HttpURLConnection,零依赖。
 * 这里只关心"什么时候结束",完整的 SSE 事件拆分/重连处理见 10.1、10.5;
 * JSON 手拼字符串构造请求体,响应用下面的极简取值函数抠字段,生产代码请用正经 JSON 库。
 */
public class ContinueSession {

    static final String API_KEY = System.getenv("EXPERT_WORK_API_KEY");
    static final String BASE_URL = "https://<your-domain>";
    static final String AGENT_CODE = "{agent_code}"; // 替换成你的 agent_code

    static String readBody(InputStream in) throws IOException {
        // InputStreamReader 必须显式指定 UTF-8——JDK 8 默认字符集跟平台走,中文会乱码
        try (BufferedReader reader = new BufferedReader(new InputStreamReader(in, StandardCharsets.UTF_8))) {
            StringBuilder sb = new StringBuilder();
            String line;
            while ((line = reader.readLine()) != null) {
                sb.append(line);
            }
            return sb.toString();
        }
    }

    static String urlEncode(String value) {
        try {
            return URLEncoder.encode(value, "UTF-8");
        } catch (UnsupportedEncodingException e) {
            throw new RuntimeException(e); // UTF-8 恒受支持,不会真的走到这里
        }
    }

    static String jsonValue(String json, String key) {
        if (json == null) {
            return null;
        }
        String needle = "\"" + key + "\"";
        int keyIdx = json.indexOf(needle);
        if (keyIdx < 0) {
            return null;
        }
        int colonIdx = json.indexOf(':', keyIdx + needle.length());
        int i = colonIdx + 1;
        while (i < json.length() && Character.isWhitespace(json.charAt(i))) {
            i++;
        }
        if (i >= json.length()) {
            return null;
        }
        char c = json.charAt(i);
        if (c == '{' || c == '[') {
            char open = c;
            char close = (c == '{') ? '}' : ']';
            int depth = 0;
            boolean inString = false;
            int start = i;
            for (; i < json.length(); i++) {
                char ch = json.charAt(i);
                if (inString) {
                    if (ch == '\\') {
                        i++;
                    } else if (ch == '"') {
                        inString = false;
                    }
                    continue;
                }
                if (ch == '"') {
                    inString = true;
                } else if (ch == open) {
                    depth++;
                } else if (ch == close) {
                    depth--;
                    if (depth == 0) {
                        i++;
                        break;
                    }
                }
            }
            return json.substring(start, i);
        } else if (c == '"') {
            int start = i + 1;
            StringBuilder sb = new StringBuilder();
            int j = start;
            while (json.charAt(j) != '"') {
                if (json.charAt(j) == '\\') {
                    sb.append(json.charAt(j + 1));
                    j += 2;
                } else {
                    sb.append(json.charAt(j));
                    j++;
                }
            }
            return sb.toString();
        } else {
            int start = i;
            while (i < json.length() && ",}] \t\n\r".indexOf(json.charAt(i)) < 0) {
                i++;
            }
            return json.substring(start, i);
        }
    }

    static List<String> splitJsonArray(String arrayText) {
        List<String> items = new ArrayList<>();
        if (arrayText == null) {
            return items;
        }
        int depth = 0;
        boolean inString = false;
        int start = -1;
        for (int i = 0; i < arrayText.length(); i++) {
            char ch = arrayText.charAt(i);
            if (inString) {
                if (ch == '\\') {
                    i++;
                } else if (ch == '"') {
                    inString = false;
                }
                continue;
            }
            if (ch == '"') {
                inString = true;
            } else if (ch == '{') {
                if (depth == 0) {
                    start = i;
                }
                depth++;
            } else if (ch == '}') {
                depth--;
                if (depth == 0) {
                    items.add(arrayText.substring(start, i + 1));
                }
            }
        }
        return items;
    }

    // JSON 字符串转义——手拼 JSON 时,插值进去的字符串必须转义,不然输入里出现一个双引号
    // (比如 input 是 他说"你好")就会把请求体拼坏,服务端解析失败(422),不是风格建议。
    // 生产代码请用 Gson / Jackson 这类正经 JSON 库自动处理,这里手写是为了保持示例零依赖。
    static String jsonEscape(String s) {
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            if (c == '"' || c == '\\') {
                sb.append('\\').append(c);
            } else if (c == '\n') {
                sb.append("\\n");
            } else if (c == '\r') {
                sb.append("\\r");
            } else if (c == '\t') {
                sb.append("\\t");
            } else if (c < 0x20) {
                sb.append(String.format("\\u%04x", (int) c));
            } else {
                sb.append(c);
            }
        }
        return sb.toString();
    }

    /**
     * 非 2xx 时真正的原因在 getErrorStream() 里,getInputStream() 只会抛一句干巴巴的状态码。
     * 必须判 null:错误响应没有 body(网关的空体 502 就是这样)时 getErrorStream() 返回
     * null,直接丢给 readBody 会当场 NPE,连状态码都跟着一起丢掉。
     */
    static String readErrorBody(HttpURLConnection connection) throws IOException {
        InputStream err = connection.getErrorStream();
        return (err == null) ? "" : readBody(err);
    }

    static String runStreamAndWait(String userId, String inputText, String sessionId) throws IOException {
        URL url = new URL(BASE_URL + "/v1/agents/" + AGENT_CODE + "/runs");
        HttpURLConnection connection = (HttpURLConnection) url.openConnection();
        connection.setRequestMethod("POST");
        connection.setRequestProperty("Authorization", "Bearer " + API_KEY);
        connection.setRequestProperty("Content-Type", "application/json");
        connection.setDoOutput(true);

        StringBuilder bodyBuilder = new StringBuilder();
        bodyBuilder.append("{\"user_id\":\"").append(jsonEscape(userId)).append("\",");
        bodyBuilder.append("\"input\":\"").append(jsonEscape(inputText)).append("\",");
        if (sessionId != null) {
            // 传了就续接这段会话,不传就开一段新会话
            bodyBuilder.append("\"session_id\":\"").append(jsonEscape(sessionId)).append("\",");
        }
        bodyBuilder.append("\"mode\":\"stream\"}");

        try (OutputStream out = connection.getOutputStream()) {
            out.write(bodyBuilder.toString().getBytes(StandardCharsets.UTF_8));
        }

        try {
            int status = connection.getResponseCode();
            if (status < 200 || status >= 300) {
                // 和 10.1 同一形态:直接 getInputStream() 只会抛一句
                // "Server returned HTTP response code: 4xx",错误码和原因得从
                // getErrorStream() 里读响应体才看得到。
                throw new IOException("创建 run 失败:" + status + " " + readErrorBody(connection));
            }

            String newSessionId = connection.getHeaderField("X-Expert-Work-Session-Id");

            try (InputStream in = connection.getInputStream()) {
                // InputStreamReader 必须显式指定 UTF-8——JDK 8 默认字符集跟平台走,中文会乱码
                Reader reader = new InputStreamReader(in, StandardCharsets.UTF_8);
                char[] chunk = new char[1024];
                StringBuilder buffer = new StringBuilder();
                int n;
                while ((n = reader.read(chunk)) != -1) {
                    buffer.append(chunk, 0, n);
                    int sep;
                    while ((sep = buffer.indexOf("\n\n")) != -1) {
                        String rawFrame = buffer.substring(0, sep);
                        buffer.delete(0, sep + 2);
                        // 只看 event: 字段的值是不是 "end",不要对整条事件文本做 "event: end"
                        // 子串匹配——服务端会流式吐 token 事件,里面是模型生成的原始文本,
                        // 如果模型回答里恰好出现这几个字符,子串匹配会被骗过,提前把
                        // "其实没跑完"的 session_id 返回给你。
                        String event = null;
                        for (String line : rawFrame.split("\n", -1)) {
                            if (line.startsWith("event:")) {
                                event = line.substring("event:".length()).trim();
                            }
                        }
                        if ("end".equals(event)) {
                            return newSessionId;
                        }
                    }
                }
            }

            return newSessionId;
        } finally {
            connection.disconnect();
        }
    }

    static String fetchLastFinalAnswer(String userId, String sessionId) throws IOException {
        String query = "user_id=" + urlEncode(userId);
        String url = BASE_URL + "/v1/agents/" + AGENT_CODE + "/sessions/" + sessionId + "/messages?" + query;
        HttpURLConnection connection = (HttpURLConnection) new URL(url).openConnection();
        connection.setRequestProperty("Authorization", "Bearer " + API_KEY);
        String responseBody;
        try {
            int status = connection.getResponseCode();
            if (status < 200 || status >= 300) {
                // 同上:404(session_id 敲错了)时 getInputStream() 抛的是一句只带 URL 的
                // FileNotFoundException,连状态码是什么、服务端说了什么都看不到。
                throw new IOException("拉取历史消息失败:" + status + " " + readErrorBody(connection));
            }
            responseBody = readBody(connection.getInputStream());
        } finally {
            connection.disconnect();
        }

        String messagesArray = jsonValue(jsonValue(responseBody, "data"), "messages");
        List<String> messages = splitJsonArray(messagesArray);
        for (int i = messages.size() - 1; i >= 0; i--) {
            String message = messages.get(i);
            if ("assistant".equals(jsonValue(message, "role")) && "final".equals(jsonValue(message, "channel"))) {
                return jsonValue(message, "content");
            }
        }
        return null;
    }

    public static void main(String[] args) throws IOException {
        String sessionId = runStreamAndWait("u-123", "你好,帮我查一下上个月的订单", null);
        System.out.println("第一轮:" + fetchLastFinalAnswer("u-123", sessionId));

        // 把上一轮拿到的 session_id 传回去,续接同一段会话
        sessionId = runStreamAndWait("u-123", "那这个月呢?", sessionId);
        System.out.println("第二轮:" + fetchLastFinalAnswer("u-123", sessionId));
    }
}
```

:::

## 10.5 断线重连(带 since_seq)

连接断了不要重新调 `POST .../runs`(那会开一轮新 run)——改用 `GET /v1/agents/{agent_code}/runs/{run_id}/events`,带上"见过的最大 seq"当 `since_seq` 重新接上。`truncated` 事件也走同一条重连路径:把它给的 `next_seq` 直接当下一次的 `since_seq`——响应头 `X-Expert-Work-Next-Seq` 也带了同一个值,下面示例统一走读事件这条路(更抗代理:有些代理/网关会丢弃或改写自定义响应头,事件本身是响应体的一部分,不会被丢)。示例里同样原样打印未分类事件的 `data`,字段含义见 [SSE 事件格式](./sse-events)。

::: code-group

```bash [curl]
# 假设上一条连接处理到 seq=41(见过的最大值)就断了,重连时带上它:
curl -N "https://<your-domain>/v1/agents/{agent_code}/runs/{run_id}/events?user_id=u-123&since_seq=41" \
  -H "Authorization: Bearer ${EXPERT_WORK_API_KEY}"
# 不带 since_seq 会从第 0 号事件起重发整个 run,断线重连务必带上这个参数
```

```python [Python]
import http.client
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

API_KEY = os.environ["EXPERT_WORK_API_KEY"]
BASE_URL = "https://<your-domain>"
AGENT_CODE = "{agent_code}"  # 替换成你的 agent_code
READ_TIMEOUT_S = 30  # 自己设的读超时,不能用默认的"无限等"
MAX_RETRIES = 5  # 重连次数上限,别无限重试——404 这类明确错误更是直接不该重试


def iter_sse_frames(response):
    """
    按行读(response.readline()),攒到一个空行(事件结束标记)才 yield 整条事件。

    **不要**改用 response.read(1024) 这种按固定字节数读的写法——`response` 是 chunked
    传输编码,Python 的 http.client 为了凑够 1024 字节,会在当前已到手的数据不够时
    反复等下一个 HTTP chunk;这条连接一旦读超时,已经到手但不满 1024 字节的数据会被
    整个丢弃(不会部分返回),表现为"服务端明明发了 metadata 事件,客户端却什么都没
    收到就超时"——这个坑比"半条事件"更隐蔽,断线重连场景下会直接导致 cursor 永远停在
    None、每次重连都不带 since_seq,和游标丢失是两个独立但会叠加的问题。
    readline() 天然按需向底层多次取数据直到凑出一整行,不会有这个"为了凑够定长反而
    卡住"的毛病。
    """
    lines = []
    while True:
        raw_line = response.readline()
        if not raw_line:
            return  # 连接关闭
        line = raw_line.decode("utf-8").rstrip("\n")
        if line == "":
            if lines:
                yield "\n".join(lines)
                lines = []
            continue
        lines.append(line)


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


class Cursor:
    """可变的游标容器——见下面 consume_one_connection 为什么必须用它而不是普通返回值。"""

    def __init__(self):
        self.value = None  # None = 还没见过任何一条事件


def consume_one_connection(response, cursor):
    """
    消费一条已建立的连接,返回是否收到了 end(真正结束)。

    cursor 用可变容器(Cursor 实例)传入、原地更新 cursor.value,而不是"处理完再一次性
    返回"——读超时是用抛异常(socket.timeout)实现的,如果 cursor 只在这个函数正常
    return 时才回传给调用方,一旦中途超时抛出,这条连接里已经确认过的 seq 会全部丢失,
    下次重连还是带着旧的 since_seq,拿到的还是这条连接里已经处理过的事件——会卡成死循环。
    用同一个 Cursor 实例原地更新,不管这个函数是正常返回还是抛异常退出,调用方读到的都是
    目前为止真实见过的最大 seq。
    """
    for raw_frame in iter_sse_frames(response):
        if not raw_frame.strip():
            continue
        event, data, seq = parse_frame(raw_frame)
        if seq is not None:
            # 见过的最大 seq,不是最后一条事件的 seq
            cursor.value = seq if cursor.value is None else max(cursor.value, seq)

        if event == "end":
            print("run 结束,status =", data["status"])
            return True
        if event == "truncated":
            print("这一页到此为止(未结束),next_seq =", data["next_seq"])
            cursor.value = data["next_seq"]  # 直接拿 next_seq 当下一次 since_seq
            return False
        if event is not None:
            print(event, data)

    return False  # 连接被关闭但没收到 end(比如读超时),外层用当前 cursor 重连


def consume_with_reconnect(user_id, run_id):
    cursor = Cursor()  # cursor.value is None = 还没见过任何一条事件,首次连接不带 since_seq
    done = False
    attempt = 0

    while not done:
        params = {"user_id": user_id}
        if cursor.value is not None:
            params["since_seq"] = cursor.value
        query = urllib.parse.urlencode(params)
        url = f"{BASE_URL}/v1/agents/{AGENT_CODE}/runs/{run_id}/events?{query}"

        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {API_KEY}"})
        seen_before = cursor.value  # 这条连接开始前的游标,用来判断它有没有真的推进
        try:
            with urllib.request.urlopen(req, timeout=READ_TIMEOUT_S) as response:
                done = consume_one_connection(response, cursor)
            if done:
                break
            # 200 + 干净读到 EOF 却没收到 end——nginx 的 proxy_read_timeout、网关/负载均衡
            # 的空闲连接回收都是这个形状:先回 200 再优雅关闭,不是 RST,所以走不到下面的
            # except。这条路和异常路径一样,算不算失败由下面那个共用的"推进过没有"说了算。
            error = RuntimeError("连接被关闭时没有推进任何 seq(可能是网关回收了空闲连接)")
        except urllib.error.HTTPError:
            # 非 2xx——服务端给出的明确错误响应,不是网络层瞬时故障,不该重试。
            # 比如 404 RUN_NOT_FOUND(run_id 敲错了)重试不会有不同结果,见 run-control.md;
            # 这种错误以前会被下面那个更宽的 except 一起接住,当成网络抖动无限重连,
            # 把平台打了——HTTPError 是 URLError 的子类,必须单独列在前面先接住它。
            raise
        except (OSError, http.client.HTTPException) as exc:
            # 读超时或连接中断——ConnectionResetError / http.client.RemoteDisconnected /
            # http.client.IncompleteRead 这些"流被中途掐断"的异常都在这里,才是真正
            # 值得重试的瞬时故障。原来这里只接 (URLError, socket.timeout),接不住这几种,
            # 断线时示例本身会崩溃(未捕获异常、退出码非 0)。
            error = exc

        # 干净 EOF 和异常断线两个出口共用下面这段。先问这条连接有没有真的推进游标:
        # 推进过就算"成功过一次",重置重试计数。这个判断必须放在两个出口的下游——只挂在
        # 干净 EOF 那一侧的话,一条"送出了事件、然后被 RST"的连接会被当成纯失败计费,已经
        # 重连成功好几次、收了十几条事件,照样报"重连 N 次仍未成功"中止掉。
        if cursor.value != seen_before:
            attempt = 0
            continue
        # 一条事件都没推进的失败才计入预算:退避 + 上限,不要无退避无上限地立刻重试,
        # 否则 attempt 永远清零,退避和上限形同虚设,会变成对着平台的重连洪水。
        attempt += 1
        if attempt > MAX_RETRIES:
            raise RuntimeError(f"重连 {MAX_RETRIES} 次仍未成功,放弃") from error
        time.sleep(min(2 ** (attempt - 1), 30))


if __name__ == "__main__":
    consume_with_reconnect("u-123", "<要重连的 run_id>")
```

```js [Node.js]
const API_KEY = process.env.EXPERT_WORK_API_KEY;
const BASE_URL = "https://<your-domain>";
const AGENT_CODE = "{agent_code}"; // 替换成你的 agent_code
const READ_TIMEOUT_MS = 30000; // 自己设的读超时,不能用默认的"无限等"
const MAX_RETRIES = 5; // 重连次数上限,别无限重试——404 这类明确错误更是直接不该重试

// 非 2xx 响应——服务端给出的明确错误,不是网络层瞬时故障,不该重试(比如 404
// RUN_NOT_FOUND,run_id 敲错了重试不会有不同结果,见 run-control.md)。用一个专门的
// 错误类型和"读超时/连接中断"这类真正值得重试的瞬时故障区分开。
class HttpStatusError extends Error {
  constructor(status, body) {
    super(`GET events failed: ${status} ${body}`);
    this.status = status;
  }
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function parseFrame(rawFrame) {
  let event = null;
  let seq = null;
  const dataLines = [];
  for (const line of rawFrame.split("\n")) {
    if (line.startsWith(":")) {
      continue; // 跳过心跳注释行
    }
    if (line.startsWith("event:")) {
      event = line.slice("event:".length).trim();
    } else if (line.startsWith("data:")) {
      dataLines.push(line.slice("data:".length).trim());
    } else if (line.startsWith("id:")) {
      const idValue = line.slice("id:".length).trim();
      seq = Number(idValue.slice(idValue.lastIndexOf("-") + 1));
    }
  }
  const data = dataLines.length > 0 ? JSON.parse(dataLines.join("\n")) : null;
  return { event, data, seq };
}

function readWithTimeout(reader, timeoutMs) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      reader.cancel().catch(() => {});
      reject(new Error(`read timed out after ${timeoutMs}ms`));
    }, timeoutMs);
    reader.read().then(
      (result) => {
        clearTimeout(timer);
        resolve(result);
      },
      (err) => {
        clearTimeout(timer);
        reject(err);
      }
    );
  });
}

/**
 * 消费一条已建立的连接,返回是否收到了 end(真正结束)。
 *
 * cursorRef 是 { value } 这样的可变容器,在读取过程中原地更新,而不是"处理完再一次性
 * 返回"——读超时是靠 readWithTimeout 抛异常实现的,如果 cursor 只在这个函数正常
 * return 时才回传给调用方,一旦中途超时抛出,这条连接里已经确认过的 seq 会全部丢失,
 * 下次重连还是带着旧的 since_seq,拿到的还是这条连接里处理过的事件——会卡成死循环。
 * 用同一个 cursorRef 对象原地更新,不管这个函数是正常返回还是抛异常退出,调用方读到的
 * 都是目前为止真实见过的最大 seq。
 */
async function consumeOneConnection(response, cursorRef) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";
  try {
    while (true) {
      const { value, done } = await readWithTimeout(reader, READ_TIMEOUT_MS);
      if (done) {
        break;
      }
      buffer += decoder.decode(value, { stream: true });
      let sepIndex;
      while ((sepIndex = buffer.indexOf("\n\n")) !== -1) {
        const rawFrame = buffer.slice(0, sepIndex);
        buffer = buffer.slice(sepIndex + 2);
        if (!rawFrame.trim()) {
          continue;
        }
        const { event, data, seq } = parseFrame(rawFrame);
        if (seq !== null) {
          // 见过的最大 seq,不是最后一条事件的 seq
          cursorRef.value = cursorRef.value === null ? seq : Math.max(cursorRef.value, seq);
        }
        if (event === "end") {
          console.log("run 结束,status =", data.status);
          return true;
        }
        if (event === "truncated") {
          console.log("这一页到此为止(未结束),next_seq =", data.next_seq);
          cursorRef.value = data.next_seq; // 直接拿 next_seq 当下一次 since_seq
          return false;
        }
        if (event !== null) {
          console.log(event, data);
        }
      }
    }
  } finally {
    // cancel() 才会真的通知底层关掉这条连接;只 releaseLock() 不会关连接——如果这个函数
    // 提前 return(比如收到 truncated)或者读超时导致外层 catch 接住,连接会一直挂着,
    // 直到 GC 或者对端超时才收场。事件端点对没跑完的 run 是长连接、服务端不设上限,
    // 生产里没有任何东西会替它关。
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
  return false; // 连接被关闭但没收到 end(比如读超时),外层用当前 cursor 重连
}

async function consumeWithReconnect(userId, runId) {
  const cursorRef = { value: null }; // value === null = 还没见过任何一条事件,首次连接不带 since_seq
  let done = false;
  let attempt = 0;

  while (!done) {
    const params = new URLSearchParams({ user_id: userId });
    if (cursorRef.value !== null) {
      params.set("since_seq", String(cursorRef.value));
    }
    const url = `${BASE_URL}/v1/agents/${AGENT_CODE}/runs/${runId}/events?${params}`;

    const seenBefore = cursorRef.value; // 这条连接开始前的游标,用来判断它有没有真的推进
    let error;
    try {
      const response = await fetch(url, {
        headers: { Authorization: `Bearer ${API_KEY}` },
      });
      if (!response.ok) {
        throw new HttpStatusError(response.status, await response.text());
      }
      done = await consumeOneConnection(response, cursorRef);
      if (done) {
        break;
      }
      // 200 + 干净读到 EOF 却没收到 end——nginx 的 proxy_read_timeout、网关/负载均衡
      // 的空闲连接回收都是这个形状:先回 200 再优雅关闭,不是 RST,所以走不到下面的
      // catch。这条路和异常路径一样,算不算失败由下面那个共用的"推进过没有"说了算。
      error = new Error("连接被关闭时没有推进任何 seq(可能是网关回收了空闲连接)");
    } catch (err) {
      if (err instanceof HttpStatusError) {
        throw err; // 明确的错误响应,不重试,直接冒泡
      }
      // 读超时或连接中断——才是真正值得重试的瞬时故障。之前这里是裸 catch {},
      // 无退避无上限地立刻重试,还会把真正的程序错误一起吞掉。
      error = err;
    }

    // 干净 EOF 和异常断线两个出口共用下面这段。先问这条连接有没有真的推进游标:
    // 推进过就算"成功过一次",重置重试计数。这个判断必须放在两个出口的下游——只挂在
    // 干净 EOF 那一侧的话,一条"送出了事件、然后被 RST"的连接会被当成纯失败计费,已经
    // 重连成功好几次、收了十几条事件,照样报"重连 N 次仍未成功"中止掉。
    if (cursorRef.value !== seenBefore) {
      attempt = 0;
      continue;
    }
    // 一条事件都没推进的失败才计入预算:退避 + 上限,超过上限就把最后一次的错误抛出去,
    // 否则 attempt 永远清零,退避和上限形同虚设,会变成对着平台的重连洪水。
    attempt++;
    if (attempt > MAX_RETRIES) {
      throw new Error(`重连 ${MAX_RETRIES} 次仍未成功,放弃`, { cause: error });
    }
    await sleep(Math.min(2 ** (attempt - 1), 30) * 1000);
  }
}

async function main() {
  await consumeWithReconnect("u-123", "<要重连的 run_id>");
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
```

```java [Java]
import java.io.IOException;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.Reader;
import java.io.UnsupportedEncodingException;
import java.net.HttpURLConnection;
import java.net.URL;
import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.util.Objects;

/**
 * 10.5 断线重连(带 since_seq) —— JDK 8 + HttpURLConnection,零依赖。
 * JSON 用下面的极简取值函数抠字段,生产代码请用 Gson / Jackson 这类正经 JSON 库,别手写 parser。
 */
public class ReconnectEvents {

    static final String API_KEY = System.getenv("EXPERT_WORK_API_KEY");
    static final String BASE_URL = "https://<your-domain>";
    static final String AGENT_CODE = "{agent_code}"; // 替换成你的 agent_code
    static final int READ_TIMEOUT_MS = 30000; // 自己设的读超时,不能用默认的"无限等"
    static final int MAX_RETRIES = 5; // 重连次数上限,别无限重试——404 这类明确错误更是直接不该重试

    /**
     * 非 2xx 响应——服务端给出的明确错误,不是网络层瞬时故障,不该重试(比如 404
     * RUN_NOT_FOUND,run_id 敲错了重试不会有不同结果,见 run-control.md)。用一个专门的
     * 异常类型和"读超时/连接中断"这类真正值得重试的瞬时故障区分开。
     */
    static class HttpStatusException extends IOException {
        private static final long serialVersionUID = 1L;

        final int status;

        HttpStatusException(int status, String body) {
            super("GET events failed: " + status + " " + body);
            this.status = status;
        }
    }

    static class Frame {
        String event;
        String rawData;
        Long seq;
    }

    static Frame parseFrame(String rawFrame) {
        Frame frame = new Frame();
        StringBuilder dataBuilder = new StringBuilder();
        boolean hasData = false;
        for (String line : rawFrame.split("\n", -1)) {
            if (line.startsWith(":")) {
                continue; // 跳过心跳注释行
            }
            if (line.startsWith("event:")) {
                frame.event = line.substring("event:".length()).trim();
            } else if (line.startsWith("data:")) {
                if (hasData) {
                    dataBuilder.append("\n");
                }
                dataBuilder.append(line.substring("data:".length()).trim());
                hasData = true;
            } else if (line.startsWith("id:")) {
                String idValue = line.substring("id:".length()).trim();
                frame.seq = Long.parseLong(idValue.substring(idValue.lastIndexOf('-') + 1));
            }
        }
        frame.rawData = hasData ? dataBuilder.toString() : null;
        return frame;
    }

    static String jsonValue(String json, String key) {
        if (json == null) {
            return null;
        }
        String needle = "\"" + key + "\"";
        int keyIdx = json.indexOf(needle);
        if (keyIdx < 0) {
            return null;
        }
        int colonIdx = json.indexOf(':', keyIdx + needle.length());
        int i = colonIdx + 1;
        while (i < json.length() && Character.isWhitespace(json.charAt(i))) {
            i++;
        }
        if (i >= json.length()) {
            return null;
        }
        char c = json.charAt(i);
        if (c == '{' || c == '[') {
            char open = c;
            char close = (c == '{') ? '}' : ']';
            int depth = 0;
            boolean inString = false;
            int start = i;
            for (; i < json.length(); i++) {
                char ch = json.charAt(i);
                if (inString) {
                    if (ch == '\\') {
                        i++;
                    } else if (ch == '"') {
                        inString = false;
                    }
                    continue;
                }
                if (ch == '"') {
                    inString = true;
                } else if (ch == open) {
                    depth++;
                } else if (ch == close) {
                    depth--;
                    if (depth == 0) {
                        i++;
                        break;
                    }
                }
            }
            return json.substring(start, i);
        } else if (c == '"') {
            int start = i + 1;
            StringBuilder sb = new StringBuilder();
            int j = start;
            while (json.charAt(j) != '"') {
                if (json.charAt(j) == '\\') {
                    sb.append(json.charAt(j + 1));
                    j += 2;
                } else {
                    sb.append(json.charAt(j));
                    j++;
                }
            }
            return sb.toString();
        } else {
            int start = i;
            while (i < json.length() && ",}] \t\n\r".indexOf(json.charAt(i)) < 0) {
                i++;
            }
            return json.substring(start, i);
        }
    }

    static String urlEncode(String value) {
        try {
            return URLEncoder.encode(value, "UTF-8");
        } catch (UnsupportedEncodingException e) {
            throw new RuntimeException(e); // UTF-8 恒受支持,不会真的走到这里
        }
    }

    /** 可变的游标容器——见下方 consumeOneConnection 为什么必须用它而不是普通返回值。 */
    static class Cursor {
        Long value; // null = 还没见过任何一条事件
    }

    /** 非 2xx 时真正的原因在 getErrorStream() 里,getInputStream() 只会抛一句干巴巴的状态码。 */
    static String readErrorBody(HttpURLConnection connection) throws IOException {
        InputStream err = connection.getErrorStream();
        if (err == null) {
            return "";
        }
        try (Reader reader = new InputStreamReader(err, StandardCharsets.UTF_8)) {
            StringBuilder sb = new StringBuilder();
            char[] buf = new char[512];
            int n;
            while ((n = reader.read(buf)) != -1) {
                sb.append(buf, 0, n);
            }
            return sb.toString();
        }
    }

    /**
     * 消费一条已建立的连接,返回是否收到了 end(真正结束)。
     *
     * cursor 用可变容器传入、原地更新,而不是"处理完再一次性返回"——读超时是用抛异常
     * (SocketTimeoutException)实现的,如果 cursor 只在方法正常 return 时才回传给调用方,
     * 一旦中途超时抛出,这条连接里已经确认过的 seq 会全部丢失,下次重连还是带着旧的
     * since_seq,拿到的还是这条连接里已经处理过的事件——会卡成死循环。用同一个 Cursor 实例
     * 原地更新,不管这个方法是正常返回还是抛异常退出,调用方读到的都是目前为止真实见过的
     * 最大 seq。
     */
    static boolean consumeOneConnection(HttpURLConnection connection, Cursor cursor) throws IOException {
        int status = connection.getResponseCode();
        if (status < 200 || status >= 300) {
            // 之前直接调 getInputStream(),非 2xx(比如 404)会抛 FileNotFoundException/
            // IOException,被下面 consumeWithReconnect 的 catch (IOException) 一起接住,
            // 当成网络抖动无限重连,把平台打了——这里先显式查状态码,非 2xx 就抛专门的
            // 异常类型,不进重连循环。
            throw new HttpStatusException(status, readErrorBody(connection));
        }
        try (InputStream in = connection.getInputStream()) {
            // InputStreamReader 必须显式指定 UTF-8——JDK 8 默认字符集跟平台走,中文会乱码
            Reader reader = new InputStreamReader(in, StandardCharsets.UTF_8);
            char[] chunk = new char[1024];
            StringBuilder buffer = new StringBuilder();
            int n;
            while ((n = reader.read(chunk)) != -1) {
                buffer.append(chunk, 0, n);
                int sep;
                while ((sep = buffer.indexOf("\n\n")) != -1) {
                    String rawFrame = buffer.substring(0, sep);
                    buffer.delete(0, sep + 2);
                    if (rawFrame.trim().isEmpty()) {
                        continue;
                    }
                    Frame frame = parseFrame(rawFrame);
                    if (frame.seq != null) {
                        // 见过的最大 seq,不是最后一条事件的 seq
                        cursor.value = (cursor.value == null) ? frame.seq : Math.max(cursor.value, frame.seq);
                    }
                    if ("end".equals(frame.event)) {
                        System.out.println("run 结束,status = " + jsonValue(frame.rawData, "status"));
                        return true;
                    }
                    if ("truncated".equals(frame.event)) {
                        cursor.value = Long.parseLong(jsonValue(frame.rawData, "next_seq"));
                        System.out.println("这一页到此为止(未结束),next_seq = " + cursor.value);
                        return false; // 直接拿 next_seq 当下一次 since_seq
                    }
                    if (frame.event != null) {
                        System.out.println(frame.event + " " + frame.rawData);
                    }
                }
            }
        }
        return false; // 连接被关闭但没收到 end(比如读超时),外层用当前 cursor 重连
    }

    static void consumeWithReconnect(String userId, String runId) throws IOException {
        Cursor cursor = new Cursor(); // cursor.value == null = 还没见过任何一条事件,首次连接不带 since_seq
        boolean done = false;
        int attempt = 0;

        while (!done) {
            StringBuilder query = new StringBuilder("user_id=").append(urlEncode(userId));
            if (cursor.value != null) {
                query.append("&since_seq=").append(cursor.value);
            }
            String url = BASE_URL + "/v1/agents/" + AGENT_CODE + "/runs/" + runId + "/events?" + query;

            HttpURLConnection connection = (HttpURLConnection) new URL(url).openConnection();
            connection.setRequestProperty("Authorization", "Bearer " + API_KEY);
            connection.setReadTimeout(READ_TIMEOUT_MS);

            Long seenBefore = cursor.value; // 这条连接开始前的游标,用来判断它有没有真的推进
            IOException error;
            try {
                done = consumeOneConnection(connection, cursor);
                if (done) {
                    break;
                }
                // 200 + 干净读到 EOF 却没收到 end——nginx 的 proxy_read_timeout、网关/负载
                // 均衡的空闲连接回收都是这个形状:先回 200 再优雅关闭,不是 RST,所以走不到
                // 下面的 catch。这条路和异常路径一样,算不算失败由下面那个共用的
                // "推进过没有"说了算。
                error = new IOException("连接被关闭时没有推进任何 seq(可能是网关回收了空闲连接)");
            } catch (HttpStatusException e) {
                throw e; // 明确的错误响应,不重试,直接冒泡
            } catch (IOException e) {
                // 读超时或连接中断——才是真正值得重试的瞬时故障。
                error = e;
            } finally {
                connection.disconnect();
            }

            // 干净 EOF 和异常断线两个出口共用下面这段。先问这条连接有没有真的推进游标:
            // 推进过就算"成功过一次",重置重试计数。这个判断必须放在两个出口的下游——只挂在
            // 干净 EOF 那一侧的话,一条"送出了事件、然后被 RST"的连接会被当成纯失败计费,已经
            // 重连成功好几次、收了十几条事件,照样报"重连 N 次仍未成功"中止掉。
            if (!Objects.equals(cursor.value, seenBefore)) {
                attempt = 0;
                continue;
            }
            // 一条事件都没推进的失败才计入预算:退避 + 上限,不要无退避无上限地立刻重试,
            // 否则 attempt 永远清零,退避和上限形同虚设,会变成对着平台的重连洪水。
            attempt++;
            if (attempt > MAX_RETRIES) {
                throw new IOException("重连 " + MAX_RETRIES + " 次仍未成功,放弃", error);
            }
            try {
                Thread.sleep(Math.min(1000L * (1L << (attempt - 1)), 30000L));
            } catch (InterruptedException ie) {
                Thread.currentThread().interrupt();
                throw error;
            }
        }
    }

    public static void main(String[] args) throws IOException {
        consumeWithReconnect("u-123", "<要重连的 run_id>");
    }
}
```

:::

## 10.6 取消 run

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
        # 但 scope 不足的 403 是裸 {"detail": ...}——读不到 error.code,完整对照表见错误码总表。
        # 先读成文本、解得动才当 JSON 用:失败响应不一定是 JSON(网关自己返回的 502 常常是
        # HTML,也可能整个是空的),无条件 json.loads 会当场抛 JSONDecodeError 顶掉原来的
        # HTTPError,状态码和响应体全看不见,下面这行想打的错误信息永远打不出来。
        error_text = exc.read().decode("utf-8", errors="replace")
        try:
            error_body = json.loads(error_text)
        except ValueError:
            error_body = error_text  # 不是 JSON,原样打出来
        print("取消失败:", exc.code, error_body)
        raise


if __name__ == "__main__":
    result = cancel_run("u-123", "<要取消的 run_id>")
    print(result)  # {"success": true, "data": {"run_id": "...", "stopped": true}, "error": null}
```

```js [Node.js]
const API_KEY = process.env.EXPERT_WORK_API_KEY;
const BASE_URL = "https://<your-domain>";
const AGENT_CODE = "{agent_code}"; // 替换成你的 agent_code

async function cancelRun(userId, runId) {
  const url = `${BASE_URL}/v1/agents/${AGENT_CODE}/runs/${runId}:cancel`;
  const response = await fetch(url, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${API_KEY}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ user_id: userId }), // user_id 在请求体,不是 query
  });
  if (!response.ok) {
    // 大多数失败响应是 {"success": false, "data": null, "error": {"code": ..., "message": ...}}
    // 但 scope 不足的 403 是裸 {"detail": ...}——读不到 error.code,完整对照表见错误码总表。
    // 这里用 text() 而不是 json():失败响应不一定是 JSON(网关自己返回的 502 常常是 HTML),
    // 先 await response.json() 会当场抛 SyntaxError,想打的错误信息反而永远打不出来。
    console.error("取消失败:", response.status, await response.text());
    throw new Error(`cancel failed: ${response.status}`);
  }
  return response.json();
}

async function main() {
  const result = await cancelRun("u-123", "<要取消的 run_id>");
  console.log(result); // {"success": true, "data": {"run_id": "...", "stopped": true}, "error": null}
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
```

```java [Java]
import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;

/**
 * 10.6 取消 run —— JDK 8 + HttpURLConnection,零依赖。
 * JSON 手拼字符串构造请求体,生产代码请用 Gson / Jackson 这类正经 JSON 库,别手拼。
 */
public class CancelRun {

    static final String API_KEY = System.getenv("EXPERT_WORK_API_KEY");
    static final String BASE_URL = "https://<your-domain>";
    static final String AGENT_CODE = "{agent_code}"; // 替换成你的 agent_code

    static String readBody(InputStream in) throws IOException {
        // InputStreamReader 必须显式指定 UTF-8——JDK 8 默认字符集跟平台走,中文会乱码
        try (BufferedReader reader = new BufferedReader(new InputStreamReader(in, StandardCharsets.UTF_8))) {
            StringBuilder sb = new StringBuilder();
            String line;
            while ((line = reader.readLine()) != null) {
                sb.append(line);
            }
            return sb.toString();
        }
    }

    /**
     * 非 2xx 时真正的原因在 getErrorStream() 里,getInputStream() 只会抛一句干巴巴的状态码。
     * 必须判 null:错误响应没有 body(网关的空体 502 就是这样)时 getErrorStream() 返回
     * null,直接丢给 readBody 会当场 NPE,连状态码都跟着一起丢掉。
     */
    static String readErrorBody(HttpURLConnection connection) throws IOException {
        InputStream err = connection.getErrorStream();
        return (err == null) ? "" : readBody(err);
    }

    // JSON 字符串转义——手拼 JSON 时,插值进去的字符串必须转义,不然输入里出现一个双引号
    // 就会把请求体拼坏,服务端解析失败(422),不是风格建议。生产代码请用 Gson / Jackson
    // 这类正经 JSON 库自动处理,这里手写是为了保持示例零依赖。
    static String jsonEscape(String s) {
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            if (c == '"' || c == '\\') {
                sb.append('\\').append(c);
            } else if (c == '\n') {
                sb.append("\\n");
            } else if (c == '\r') {
                sb.append("\\r");
            } else if (c == '\t') {
                sb.append("\\t");
            } else if (c < 0x20) {
                sb.append(String.format("\\u%04x", (int) c));
            } else {
                sb.append(c);
            }
        }
        return sb.toString();
    }

    static String cancelRun(String userId, String runId) throws IOException {
        URL url = new URL(BASE_URL + "/v1/agents/" + AGENT_CODE + "/runs/" + runId + ":cancel");
        HttpURLConnection connection = (HttpURLConnection) url.openConnection();
        connection.setRequestMethod("POST");
        connection.setRequestProperty("Authorization", "Bearer " + API_KEY);
        connection.setRequestProperty("Content-Type", "application/json");
        connection.setDoOutput(true);

        String body = "{\"user_id\":\"" + jsonEscape(userId) + "\"}"; // user_id 在请求体,不是 query
        try (OutputStream out = connection.getOutputStream()) {
            out.write(body.getBytes(StandardCharsets.UTF_8));
        }

        try {
            int status = connection.getResponseCode();
            if (status >= 200 && status < 300) {
                return readBody(connection.getInputStream());
            }

            // 大多数失败响应是 {"success": false, "data": null, "error": {"code": ..., "message": ...}}
            // 但 scope 不足的 403 是裸 {"detail": ...}——读不到 error.code,完整对照表见错误码总表
            System.out.println("取消失败:" + status + " " + readErrorBody(connection));
            throw new IOException("cancel failed: " + status);
        } finally {
            connection.disconnect();
        }
    }

    public static void main(String[] args) throws IOException {
        String result = cancelRun("u-123", "<要取消的 run_id>");
        System.out.println(result); // {"success": true, "data": {"run_id": "...", "stopped": true}, "error": null}
    }
}
```

:::

## 10.7 审批决策

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

```js [Node.js]
const API_KEY = process.env.EXPERT_WORK_API_KEY;
const BASE_URL = "https://<your-domain>";
const AGENT_CODE = "{agent_code}"; // 替换成你的 agent_code

async function decideRun(userId, runId, decision, modifiedArgs, reason) {
  const url = `${BASE_URL}/v1/agents/${AGENT_CODE}/runs/${runId}:decide`;
  const body = { user_id: userId, decision, mode: "queue" };
  if (decision === "modify") {
    body.modified_args = modifiedArgs || {}; // 仅 modify 时必填,其余两种 decision 下禁止传
  }
  if (reason !== undefined) {
    body.reason = reason;
  }

  const response = await fetch(url, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${API_KEY}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    // 用 text() 而不是 json():失败响应不一定是 JSON(网关自己返回的 502 常常是 HTML),
    // 先 await response.json() 会当场抛 SyntaxError,想打的错误信息反而永远打不出来。
    console.error("决策失败:", response.status, await response.text());
    throw new Error(`decide failed: ${response.status}`);
  }
  const payload = await response.json();
  const newRunId = response.headers.get("X-Expert-Work-Run-Id"); // 续跑用的新 run_id,不是路径里那个
  return { payload, newRunId };
}

async function main() {
  let { payload, newRunId } = await decideRun("u-123", "<待审批的 run_id>", "approve");
  console.log("续跑的新 run_id:", newRunId);
  console.log(payload);

  ({ payload, newRunId } = await decideRun("u-123", "<另一个待审批的 run_id>", "reject", undefined, "超出预算范围"));
  console.log(payload);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
```

```java [Java]
import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;

/**
 * 10.7 审批决策 —— JDK 8 + HttpURLConnection,零依赖。
 * JSON 手拼字符串构造请求体,生产代码请用 Gson / Jackson 这类正经 JSON 库,别手拼。
 */
public class ApprovalDecision {

    static final String API_KEY = System.getenv("EXPERT_WORK_API_KEY");
    static final String BASE_URL = "https://<your-domain>";
    static final String AGENT_CODE = "{agent_code}"; // 替换成你的 agent_code

    static String readBody(InputStream in) throws IOException {
        // InputStreamReader 必须显式指定 UTF-8——JDK 8 默认字符集跟平台走,中文会乱码
        try (BufferedReader reader = new BufferedReader(new InputStreamReader(in, StandardCharsets.UTF_8))) {
            StringBuilder sb = new StringBuilder();
            String line;
            while ((line = reader.readLine()) != null) {
                sb.append(line);
            }
            return sb.toString();
        }
    }

    // JSON 字符串转义——手拼 JSON 时,插值进去的字符串必须转义,不然输入里出现一个双引号
    // (比如 reason 是 超预算,原因是"临时项目")就会把请求体拼坏,服务端解析失败(422),
    // 不是风格建议。生产代码请用 Gson / Jackson 这类正经 JSON 库自动处理,这里手写是为了
    // 保持示例零依赖。注意 modifiedArgsJson 不走这个函数——它本身就是调用方传进来的一段
    // 已经拼好的 JSON 对象文本,原样拼进请求体,再转义一遍反而会转义出双重转义。
    static String jsonEscape(String s) {
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            if (c == '"' || c == '\\') {
                sb.append('\\').append(c);
            } else if (c == '\n') {
                sb.append("\\n");
            } else if (c == '\r') {
                sb.append("\\r");
            } else if (c == '\t') {
                sb.append("\\t");
            } else if (c < 0x20) {
                sb.append(String.format("\\u%04x", (int) c));
            } else {
                sb.append(c);
            }
        }
        return sb.toString();
    }

    /**
     * 非 2xx 时真正的原因在 getErrorStream() 里,getInputStream() 只会抛一句干巴巴的状态码。
     * 必须判 null:错误响应没有 body(网关的空体 502 就是这样)时 getErrorStream() 返回
     * null,直接丢给 readBody 会当场 NPE,连状态码都跟着一起丢掉。
     */
    static String readErrorBody(HttpURLConnection connection) throws IOException {
        InputStream err = connection.getErrorStream();
        return (err == null) ? "" : readBody(err);
    }

    /**
     * 返回 [响应体 JSON 文本, 续跑用的新 run_id]。decision 为 "modify" 时 modifiedArgsJson 必填
     * (直接传一段手拼好的 JSON 对象文本);其余两种 decision 下必须传 null——不传的字段不会出现在请求体里。
     */
    static String[] decideRun(String userId, String runId, String decision, String modifiedArgsJson, String reason)
            throws IOException {
        URL url = new URL(BASE_URL + "/v1/agents/" + AGENT_CODE + "/runs/" + runId + ":decide");
        HttpURLConnection connection = (HttpURLConnection) url.openConnection();
        connection.setRequestMethod("POST");
        connection.setRequestProperty("Authorization", "Bearer " + API_KEY);
        connection.setRequestProperty("Content-Type", "application/json");
        connection.setDoOutput(true);

        StringBuilder bodyBuilder = new StringBuilder();
        bodyBuilder.append("{\"user_id\":\"").append(jsonEscape(userId)).append("\",");
        bodyBuilder.append("\"decision\":\"").append(jsonEscape(decision)).append("\",");
        if ("modify".equals(decision)) {
            // 仅 modify 时必填,其余两种 decision 下禁止传
            bodyBuilder.append("\"modified_args\":").append(modifiedArgsJson).append(",");
        }
        if (reason != null) {
            bodyBuilder.append("\"reason\":\"").append(jsonEscape(reason)).append("\",");
        }
        bodyBuilder.append("\"mode\":\"queue\"}");

        try (OutputStream out = connection.getOutputStream()) {
            out.write(bodyBuilder.toString().getBytes(StandardCharsets.UTF_8));
        }

        try {
            int status = connection.getResponseCode();
            if (status < 200 || status >= 300) {
                // 和 10.6 取消同一形态:失败响应可能是带 error.code 的 JSON、可能是裸
                // {"detail": ...},也可能是网关自己返回的 HTML 甚至空体,一律当文本原样打出来。
                System.out.println("决策失败:" + status + " " + readErrorBody(connection));
                throw new IOException("decide failed: " + status);
            }
            String responseBody = readBody(connection.getInputStream());
            String newRunId = connection.getHeaderField("X-Expert-Work-Run-Id"); // 续跑用的新 run_id,不是路径里那个
            return new String[] {responseBody, newRunId};
        } finally {
            connection.disconnect();
        }
    }

    public static void main(String[] args) throws IOException {
        String[] approveResult = decideRun("u-123", "<待审批的 run_id>", "approve", null, null);
        System.out.println("续跑的新 run_id:" + approveResult[1]);
        System.out.println(approveResult[0]);

        String[] rejectResult = decideRun("u-123", "<另一个待审批的 run_id>", "reject", null, "超出预算范围");
        System.out.println(rejectResult[0]);
    }
}
```

:::
