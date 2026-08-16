import { defineConfig } from "vitepress";

export default defineConfig({
  lang: "zh-CN",
  title: "Expert-Work 开放 API",
  description: "第三方对接文档",
  base: "/docs/",
  themeConfig: {
    nav: [{ text: "指南", link: "/guide/quickstart" }],
    sidebar: [
      {
        text: "对接指南",
        items: [
          { text: "1 概述与对接流程", link: "/guide/quickstart" },
          {
            text: "2 通用约定",
            link: "/guide/conventions",
            items: [
              { text: "2.1 环境地址", link: "/guide/conventions#_2-1-环境地址" },
              { text: "2.2 协议约定", link: "/guide/conventions#_2-2-协议约定" },
              { text: "2.3 公共请求头", link: "/guide/conventions#_2-3-公共请求头" },
              { text: "2.4 统一响应格式", link: "/guide/conventions#_2-4-统一响应格式" },
              { text: "2.5 限流与配额", link: "/guide/conventions#_2-5-限流与配额" },
              { text: "2.6 幂等性", link: "/guide/conventions#_2-6-幂等性" },
            ],
          },
          {
            text: "3 认证",
            link: "/guide/auth",
            items: [
              { text: "3.1 服务账号与 Key", link: "/guide/auth#_3-1-服务账号与-key-持钥人与钥匙" },
              { text: "3.2 Key 长什么样", link: "/guide/auth#_3-2-key-长什么样" },
              { text: "3.3 Scope 怎么选", link: "/guide/auth#_3-3-scope-怎么选" },
              { text: "3.4 创建一把 Key", link: "/guide/auth#_3-4-创建一把-key" },
              { text: "3.5 过期语义", link: "/guide/auth#_3-5-过期语义" },
              { text: "3.6 轮换与吊销", link: "/guide/auth#_3-6-轮换与吊销" },
              { text: "3.7 Key 失效时会发生什么", link: "/guide/auth#_3-7-key-失效时会发生什么" },
            ],
          },
          {
            text: "4 接口详情",
            link: "/guide/run-agent",
            items: [
              { text: "Agent 目录", link: "/guide/run-agent#agent-目录" },
              { text: "run 列表", link: "/guide/run-agent#run-列表" },
            ],
          },
          {
            text: "4.2/4.7 取消 run 与审批决策",
            link: "/guide/run-control",
            items: [
              { text: "4.2 取消 run", link: "/guide/run-control#_4-2-取消-run" },
              { text: "4.7 审批决策", link: "/guide/run-control#_4-7-审批决策" },
            ],
          },
          {
            text: "5 SSE 事件格式",
            link: "/guide/sse-events",
            items: [
              { text: "5.1 帧格式", link: "/guide/sse-events#_5-1-帧格式" },
              { text: "5.2 事件总表", link: "/guide/sse-events#_5-2-事件总表" },
              { text: "5.3 updates 帧怎么解析", link: "/guide/sse-events#_5-3-updates-帧怎么解析" },
              { text: "5.4 token 帧", link: "/guide/sse-events#_5-4-token-帧" },
              { text: "5.5 worker / guard / compaction 帧", link: "/guide/sse-events#_5-5-worker-guard-compaction-帧" },
              { text: "5.6 断线重连", link: "/guide/sse-events#_5-6-断线重连" },
            ],
          },
          { text: "6 错误码总表", link: "/guide/errors" },
          {
            text: "7 对接注意事项与 FAQ",
            link: "/guide/best-practices",
            items: [
              { text: "7.1 只在服务端调用", link: "/guide/best-practices#_7-1-只在服务端调用-别把-key-塞进前端" },
              { text: "7.2 user_id 怎么取", link: "/guide/best-practices#_7-2-user-id-怎么取" },
              { text: "7.3 Key 的保管与轮换", link: "/guide/best-practices#_7-3-key-的保管与轮换" },
              { text: "7.4 Stream 断线怎么处理", link: "/guide/best-practices#_7-4-stream-断线怎么处理" },
              { text: "7.5 常见问题", link: "/guide/best-practices#_7-5-常见问题" },
              { text: "7.6 联调自测清单", link: "/guide/best-practices#_7-6-联调自测清单" },
            ],
          },
          { text: "8 附录:多语言示例", link: "/guide/examples" },
        ],
      },
    ],
    search: { provider: "local" },
    outline: { label: "本页目录" },
    footer: {
      message: "对外 API 版本 v1",
      copyright: "文档更新于 2026-08-14",
    },
  },
});
