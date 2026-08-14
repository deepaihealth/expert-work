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
          { text: "2 通用约定", link: "/guide/conventions" },
          { text: "3 认证", link: "/guide/auth" },
          { text: "4 接口详情", link: "/guide/run-agent" },
          { text: "4.2/4.7 取消 run 与审批决策", link: "/guide/run-control" },
          { text: "5 SSE 事件格式", link: "/guide/sse-events" },
          { text: "6 错误码总表", link: "/guide/errors" },
          { text: "7 对接注意事项与 FAQ", link: "/guide/best-practices" },
        ],
      },
    ],
    search: { provider: "local" },
    outline: { label: "本页目录" },
  },
});
