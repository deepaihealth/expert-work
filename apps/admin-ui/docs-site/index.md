---
layout: home

hero:
  name: Expert-Work 开放 API
  text: 把 Agent 接进你的产品
  tagline: 一个 API Key、一套 REST 接口、一条 SSE 事件流——发起对话、接收流式回答、管理会话与产物文件。面向第三方开发工程师的对接文档。
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
  - icon: { light: /icons/chat-light.svg, dark: /icons/chat-dark.svg }
    title: 跟 Agent 对话
    details: 发起对话的完整参数、stream 与 queue 两种模式、多轮会话、带图片和文档、防重复下发。
    link: /guide/chat
    linkText: 查看参数表
  - icon: { light: /icons/stream-light.svg, dark: /icons/stream-dark.svg }
    title: 读懂 SSE 流
    details: 每个事件的字段与处理方式、可直接抄的接收器骨架、断线重连与续传。
    link: /guide/sse-events
    linkText: 解析事件流
  - icon: { light: /icons/control-light.svg, dark: /icons/control-dark.svg }
    title: 对话过程中的控制
    details: 中途取消一次 run，以及 run 停在人工审批节点时怎么提交决策。
    link: /guide/run-control
    linkText: 取消与审批
  - icon: { light: /icons/query-light.svg, dark: /icons/query-dark.svg }
    title: 查询与管理
    details: 有哪些 Agent 可调、会话与历史消息、执行记录、下载 Agent 产出的文件。
    link: /guide/query
    linkText: 查询接口
  - icon: { light: /icons/auth-light.svg, dark: /icons/auth-dark.svg }
    title: 认证与 Key
    details: 服务账号与 API Key、权限档位、请求头写法、轮换与撤销。
    link: /guide/auth
    linkText: 认证方式
  - icon: { light: /icons/conv-light.svg, dark: /icons/conv-dark.svg }
    title: 通用约定
    details: 接口地址与版本、响应结构、分页、限流与配额、每种响应都带的响应头。
    link: /guide/conventions
    linkText: 通用约定
  - icon: { light: /icons/errors-light.svg, dark: /icons/errors-dark.svg }
    title: 错误码总表
    details: 按状态码查错误、按错误码查含义，每一条都写明该怎么处理。
    link: /guide/errors
    linkText: 错误码
  - icon: { light: /icons/code-light.svg, dark: /icons/code-dark.svg }
    title: 多语言示例
    details: curl、Python、Node.js、Java 四种写法，覆盖发起对话到审批决策七个场景，可直接运行。
    link: /guide/examples
    linkText: 看示例
---
