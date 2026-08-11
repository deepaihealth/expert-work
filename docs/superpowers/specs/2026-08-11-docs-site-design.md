# 文档站点设计(三视角:公开 API / 租户 / 内部运维)

> 2026-08-11 brainstorm 定稿。范围:一个公开 API 文档站 + admin-ui 应用内文档页。全中文,多语言留后。

## 一、背景与目标

平台缺 web 形式文档。三类读者、三档权限(用户拍板):

| 视角 | 读者 | 权限 |
|---|---|---|
| 第三方 API 文档 | 对接方工程师 | 完全公开 |
| 使用手册 | 租户自己的工程师 | 登录成员可见 |
| 平台运维 | 内部运维工程师 | 仅 system_admin |

方案选型(A/B/C 里定 A):**权限复用现有设施**——公开站纯静态,受限文档进 admin-ui(登录门 + 角色显隐/路由守卫),不新增认证面(oauth2-proxy/文档后端均否决:多一处认证面 = 多一处出洞的地方,而受众规模撑不起这个成本)。

## 二、总体架构

```
expert-work-test.deepaihealth.com
├── /docs/          → 公开 API 文档站(VitePress 静态产物,admin-ui 镜像的 nginx 多一个 location)
└── admin-ui 应用内  → 「文档」菜单页(markdown 打包进前端 bundle)
     ├── 使用手册    → 所有登录成员可见
     └── 平台运维    → 仅 system_admin(菜单显隐 + 路由守卫双重)
```

- 公开站不占新域名、不动 DNS/证书:走现有域名 `/docs/` 路径。
- **明示取舍(用户认可)**:应用内文档打进前端 bundle,懂行的登录用户理论上能从 JS 资产扒到运维文档文本。防的是「不该看到入口」,不是拜占庭对抗;**凭据、集群连接串、密钥名等机密一律不得写入文档**——写进去本身就是事故,评审要专门盯这条。

## 三、公开 API 文档站

- **技术**:VitePress(默认主题,内建本地搜索),源码 `docs-site/`,独立 `package.json`。
- **构建链**:admin-ui Dockerfile 多一个 build stage(`npm ci && npm run build`)→ 产物 COPY 到 nginx `/usr/share/nginx/html/docs/`;nginx.conf 加 `location /docs/ { try_files … }`,不影响 SPA fallback。
- **本地**:`cd docs-site && npm run dev` 热预览。
- **初版篇目**(全新撰写,事实来自代码与本会话验证过的行为):
  1. 快速开始——拿 key → 首次调用 5 分钟跑通
  2. 认证——服务账号 / API key / scope 三档(read/write/admin)/ 轮换与回显
  3. 调用 Agent——`POST /v1/agents/{agent_code}/runs` 全参数;`mode: stream|queue`;`session_id` 续会话;`user_id` 语义(自动铸终端用户,记忆/工作区/计费 key 其上)
  4. SSE 事件格式——改写 `docs/api/streaming-events.md`
  5. 错误码与限流——401/403/404/413/429 语义、配额行为(工作区 429 文案等)
  6. 最佳实践——服务端调用(CORS/key 泄露)、user_id 设计、key 保管与轮换

## 四、应用内文档页

- **内容源**:`apps/admin-ui/src/docs/{tenant,ops}/*.md`,Vite `import.meta.glob("...", { query: "?raw" })` 打包;每篇 front-matter:`title` / `order` / (可选)`group`。
- **渲染**:复用既有依赖 `markdown-to-jsx`(ToolTimeline 在用),不新增渲染库。
- **页面**:导航加「文档」入口(navModel);左目录树右正文;树由 front-matter 生成。
- **角色**:`system_admin` 见「平台运维」组;普通成员只见「使用手册」。菜单显隐 + 路由守卫双判(admin-ui 已有 system_admin 判定,照既有页面先例)。
- **测试**:vitest——①member 角色渲染不出运维组(含直接路由访问被挡);②admin 两组都见;③md 渲染冒烟(front-matter 解析 + 标题出现)。变异自证仓库铁律照走。

## 五、内容清单初版(控制器写初稿,用户审)

**使用手册(tenant/)**:平台概览与登录(含首登改密)/ 成员与角色(邀请、重置密码、停用)/ Agent 配置(对配置页写)/ 服务账号与 API 密钥(建号、发 key、回显、轮换)/ 会话与调试台 / 配额与用量。

**平台运维(ops/)**:租户生命周期(建租户、首管开通、配额、停用)/ 用户维度运维(工作区、记忆治理、删用户链路)/ 发布流程(release.sh 三线、smoke、记录 PR 惯例)/ 备份与恢复(OSS 归档、NAS 回收站、90 天语义)/ 常见故障排查(janitor、沙箱、观测栈精选)。

运维篇改写自现有 runbooks——**只保留操作语义,剔除凭据路径、内网地址、金库名等**;不逐字搬运。

## 六、更新与验收

- 文档随普通 PR 走(代码同一评审流);发布随 admin-ui 线(`release.sh test --images admin-ui`)。
- CI:admin-ui workflow 加 `docs-site` build(挡坏链/坏语法);既有 vitest 流程覆盖新页。
- smoke.sh 加 `/docs/` 200 探针。
- 真栈验收:system_admin 登录见两组;租户成员登录只见使用手册且直连运维路由被挡;无痕窗口开 `/docs/` 全站可浏览可搜索。

## 七、不做什么

- 不做多语言(后需再做,VitePress 与前端 i18n 都有现成扩展点)。
- 不做文档版本化(单版本随 main)。
- 不做评论/反馈组件。
- 不做 OpenAPI 自动生成 reference(FastAPI schema 与对外口径差距大,手写六篇更准;后续可补)。
- 内部运维文档不追求 bundle 级保密(见 § 二取舍)。
