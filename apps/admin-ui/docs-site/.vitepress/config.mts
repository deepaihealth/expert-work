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
          { text: "快速开始", link: "/guide/quickstart" },
          { text: "认证", link: "/guide/auth" },
          { text: "调用 Agent", link: "/guide/run-agent" },
          { text: "SSE 事件格式", link: "/guide/sse-events" },
          { text: "错误码与限流", link: "/guide/errors" },
          { text: "最佳实践", link: "/guide/best-practices" },
        ],
      },
    ],
    search: { provider: "local" },
    outline: { label: "本页目录" },
  },
});
