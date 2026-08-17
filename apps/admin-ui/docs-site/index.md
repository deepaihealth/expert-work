---
layout: home

hero:
  name: Expert-Work 开放 API
  text: 第三方对接文档
  tagline: 用一个 API Key 把你的应用接入 Expert-Work 的 Agent——发起对话、拿到流式回答、管理会话与产出文件。
  actions:
    - theme: brand
      text: 五分钟跑通第一次调用
      link: /guide/quickstart
    - theme: alt
      text: 跟 Agent 对话
      link: /guide/chat
    - theme: alt
      text: 多语言示例
      link: /guide/examples

features:
  - title: 1 快速开始
    details: 拿到 key、发一条 curl、看到 SSE 流回来，再接着聊下一轮。
    link: /guide/quickstart
    linkText: 开始接入
  - title: 2 跟 Agent 对话
    details: 发起对话的完整参数、stream 与 queue 两种模式、多轮会话、带图片和文档、防重复下发。
    link: /guide/chat
    linkText: 查看参数表
  - title: 3 读懂 SSE 流
    details: 事件一览与逐个事件的字段、前端渲染示例、可直接抄的接收器骨架、断线重连与回放分页。
    link: /guide/sse-events
    linkText: 解析事件流
  - title: 4 对话过程中的控制
    details: 中途取消一次 run、以及 run 停在人工审批节点时怎么提交决策。
    link: /guide/run-control
    linkText: 取消与审批
  - title: 5 查询与管理
    details: 有哪些 Agent 可调、会话与历史消息、执行记录、下载 Agent 产出的文件。
    link: /guide/query
    linkText: 查询接口
---
