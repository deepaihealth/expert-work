import { defineConfig } from "vitepress";
import { withMermaid } from "vitepress-plugin-mermaid";

export default withMermaid(
  defineConfig({
    lang: "zh-CN",
    title: "Expert-Work 开放 API",
    description: "第三方对接文档",
    base: "/docs/",
    themeConfig: {
      nav: [
        { text: "快速开始", link: "/guide/quickstart" },
        { text: "跟 Agent 对话", link: "/guide/chat" },
        { text: "示例代码", link: "/guide/examples" },
      ],
      sidebar: [
        {
          text: "上手",
          items: [
            {
              text: "1 快速开始",
              link: "/guide/quickstart",
              items: [
                { text: "1.1 拿一把 Key", link: "/guide/quickstart#_1-1-拿一把-key" },
                { text: "1.2 发起第一次对话", link: "/guide/quickstart#_1-2-发起第一次对话" },
                { text: "1.3 读懂返回的流", link: "/guide/quickstart#_1-3-读懂返回的流" },
                { text: "1.4 接着聊下一轮", link: "/guide/quickstart#_1-4-接着聊下一轮" },
              ],
            },
          ],
        },
        {
          text: "核心接口",
          items: [
            {
              text: "2 跟 Agent 对话",
              link: "/guide/chat",
              items: [
                { text: "2.1 一次对话是怎么走的", link: "/guide/chat#_2-1-一次对话是怎么走的" },
                { text: "2.2 发起对话", link: "/guide/chat#_2-2-发起对话" },
                { text: "2.3 请求参数详解", link: "/guide/chat#_2-3-请求参数详解" },
                { text: "2.4 stream 还是 queue", link: "/guide/chat#_2-4-stream-还是-queue" },
                { text: "2.5 多轮会话", link: "/guide/chat#_2-5-多轮会话" },
                { text: "2.6 带图片和文档", link: "/guide/chat#_2-6-带图片和文档" },
                { text: "2.7 外部内容与模板变量", link: "/guide/chat#_2-7-外部内容与模板变量" },
                { text: "2.8 防重复下发", link: "/guide/chat#_2-8-防重复下发-idempotency-key" },
              ],
            },
            {
              text: "3 读懂 SSE 流",
              link: "/guide/sse-events",
              items: [
                { text: "3.1 帧格式", link: "/guide/sse-events#_3-1-帧格式" },
                { text: "3.2 事件总表", link: "/guide/sse-events#_3-2-事件总表" },
                { text: "3.3 updates 帧怎么解析", link: "/guide/sse-events#_3-3-updates-帧怎么解析" },
                { text: "3.4 token 帧", link: "/guide/sse-events#_3-4-token-帧" },
                { text: "3.5 worker / guard / compaction 帧", link: "/guide/sse-events#_3-5-worker-guard-compaction-帧" },
                { text: "3.6 断线重连", link: "/guide/sse-events#_3-6-断线重连" },
              ],
            },
            {
              text: "4 对话过程中的控制",
              link: "/guide/run-control",
              items: [
                { text: "4.1 取消 run", link: "/guide/run-control#_4-1-取消-run" },
                { text: "4.2 审批决策", link: "/guide/run-control#_4-2-审批决策" },
              ],
            },
            {
              text: "5 查询与管理",
              link: "/guide/query",
              items: [
                { text: "5.1 Agent 目录", link: "/guide/query#_5-1-agent-目录" },
                { text: "5.2 会话列表", link: "/guide/query#_5-2-会话列表" },
                { text: "5.3 历史消息", link: "/guide/query#_5-3-历史消息" },
                { text: "5.4 run 列表", link: "/guide/query#_5-4-run-列表" },
                { text: "5.5 重命名与归档", link: "/guide/query#_5-5-重命名与归档" },
                { text: "5.6 工作区文件", link: "/guide/query#_5-6-工作区文件" },
                { text: "5.7 产物", link: "/guide/query#_5-7-产物" },
              ],
            },
          ],
        },
        {
          text: "参考",
          items: [
            {
              text: "6 认证与 Key",
              link: "/guide/auth",
              items: [
                { text: "6.1 服务账号与 API Key", link: "/guide/auth#_6-1-服务账号与-api-key" },
                { text: "6.2 Key 的格式", link: "/guide/auth#_6-2-key-的格式" },
                { text: "6.3 三档权限怎么选", link: "/guide/auth#_6-3-三档权限怎么选" },
                { text: "6.4 创建一把 Key", link: "/guide/auth#_6-4-创建一把-key" },
                { text: "6.5 过期时间", link: "/guide/auth#_6-5-过期时间" },
                { text: "6.6 轮换与吊销", link: "/guide/auth#_6-6-轮换与吊销" },
                { text: "6.7 Key 失效时会发生什么", link: "/guide/auth#_6-7-key-失效时会发生什么" },
              ],
            },
            {
              text: "7 通用约定",
              link: "/guide/conventions",
              items: [
                { text: "7.1 环境地址", link: "/guide/conventions#_7-1-环境地址" },
                { text: "7.2 协议约定", link: "/guide/conventions#_7-2-协议约定" },
                { text: "7.3 公共请求头", link: "/guide/conventions#_7-3-公共请求头" },
                { text: "7.4 响应头", link: "/guide/conventions#_7-4-响应头" },
                { text: "7.5 统一响应格式", link: "/guide/conventions#_7-5-统一响应格式" },
                { text: "7.6 限流与配额", link: "/guide/conventions#_7-6-限流与配额" },
                { text: "7.7 幂等性", link: "/guide/conventions#_7-7-幂等性" },
              ],
            },
            {
              text: "8 错误码总表",
              link: "/guide/errors",
              items: [
                { text: "8.1 错误码速查表", link: "/guide/errors#_8-1-错误码速查表" },
                { text: "8.2 错误响应的形状不统一", link: "/guide/errors#_8-2-错误响应的形状不统一" },
                { text: "8.3 – 8.15 按状态码分节", link: "/guide/errors" },
                { text: "8.16 限流与配额的区别", link: "/guide/errors#_8-16-限流与配额的区别" },
              ],
            },
            {
              text: "9 对接注意事项与 FAQ",
              link: "/guide/best-practices",
              items: [
                { text: "9.1 只在服务端调用", link: "/guide/best-practices#_9-1-只在服务端调用-别把-key-放进前端" },
                { text: "9.2 user_id 怎么取", link: "/guide/best-practices#_9-2-user-id-怎么取" },
                { text: "9.3 Key 的保管与轮换", link: "/guide/best-practices#_9-3-key-的保管与轮换" },
                { text: "9.4 Stream 断线怎么处理", link: "/guide/best-practices#_9-4-stream-断线怎么处理" },
                { text: "9.5 常见问题", link: "/guide/best-practices#_9-5-常见问题" },
                { text: "9.6 联调自测清单", link: "/guide/best-practices#_9-6-联调自测清单" },
              ],
            },
            { text: "10 多语言示例", link: "/guide/examples" },
          ],
        },
      ],
      search: { provider: "local" },
      outline: { label: "本页目录", level: [2, 3] },
      footer: {
        message: "对外 API 版本 v1",
        copyright: "文档更新于 2026-08-16",
      },
    },
    markdown: {
      config(md) {
        const fence = md.renderer.rules.fence!
        md.renderer.rules.fence = (tokens, idx, options, env, self) => {
          // VitePress 内置 preWrapperPlugin 会在渲染时把 [title] 从 token.info 剥掉
          // (副作用发生在下面这次 fence() 调用内部),所以必须在调用前先取值
          const token = tokens[idx]
          const m = /\[([^\]]+)\]/.exec(token.info)
          const html = fence(tokens, idx, options, env, self)
          if (!m) return html
          // 在 ::: code-group 里,[title] 已经是 tab 名,不再重复画标题栏
          for (let i = idx - 1; i >= 0; i--) {
            const t = tokens[i]
            if (t.type === 'container_code-group_open') return html
            if (t.type === 'container_code-group_close') break
          }
          const title = md.utils.escapeHtml(m[1])
          return html.replace(
            /^(<div class="language-[^"]*"[^>]*>)/,
            `$1<div class="ew-code-title">${title}</div>`
          )
        }
      },
    },
  }),
);
