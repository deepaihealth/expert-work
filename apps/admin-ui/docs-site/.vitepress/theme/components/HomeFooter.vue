<script setup lang="ts">
import { withBase } from 'vitepress'
// 首页页脚:章节索引 / 对接支持 / 版本。只在首页出现(features 之后)。
const chapters = [
  ['1 快速开始', '/guide/quickstart'],
  ['2 跟 Agent 对话', '/guide/chat'],
  ['3 读懂 SSE 流', '/guide/sse-events'],
  ['4 对话过程中的控制', '/guide/run-control'],
  ['5 查询与管理', '/guide/query'],
  ['6 认证与 Key', '/guide/auth'],
  ['7 通用约定', '/guide/conventions'],
  ['8 错误码总表', '/guide/errors'],
  ['9 对接注意事项', '/guide/best-practices'],
  ['10 多语言示例', '/guide/examples'],
]
</script>

<template>
  <section class="ew-foot">
    <div class="ew-foot__inner">
      <div class="ew-foot__brand">
        <img :src="withBase('/logo.svg')" alt="" width="28" height="28" />
        <div>
          <div class="ew-foot__name">Expert-Work 开放 API</div>
          <div class="ew-foot__sub">面向第三方开发工程师的对接文档</div>
        </div>
      </div>
      <div class="ew-foot__cols">
        <div class="ew-foot__col ew-foot__col--wide">
          <h4>章节</h4>
          <ul class="ew-foot__grid">
            <li v-for="[t, l] in chapters" :key="l"><a :href="withBase(l)">{{ t }}</a></li>
          </ul>
        </div>
        <div class="ew-foot__col">
          <h4>对接支持</h4>
          <ul>
            <li><a :href="withBase('/guide/best-practices')">上线前自测清单</a></li>
            <li><a :href="withBase('/guide/errors')">错误码怎么处理</a></li>
            <li><a :href="withBase('/guide/auth#_6-1-服务账号与-api-key')">Key 由租户管理员发放</a></li>
          </ul>
        </div>
        <div class="ew-foot__col">
          <h4>版本</h4>
          <ul>
            <li>对外 API v1</li>
            <li>SSE 事件契约见第 3 章</li>
            <li>无 SDK 依赖，任何语言可接</li>
          </ul>
        </div>
      </div>
    </div>
  </section>
</template>

<style scoped>
.ew-foot { padding: 48px 24px 8px; }
.ew-foot__inner { max-width: 1152px; margin: 0 auto; padding-top: 32px; border-top: 1px solid var(--vp-c-divider); display: grid; grid-template-columns: 260px 1fr; gap: 32px; }
.ew-foot__brand { display: flex; gap: 12px; align-items: flex-start; }
.ew-foot__brand img { border-radius: 7px; margin-top: 2px; }
.ew-foot__name { font-weight: 650; color: var(--vp-c-text-1); }
.ew-foot__sub { margin-top: 2px; font-size: 13px; color: var(--vp-c-text-3); }
.ew-foot__cols { display: grid; grid-template-columns: 2fr 1fr 1fr; gap: 24px; }
.ew-foot__col h4 { margin: 0 0 10px; font-size: 12px; font-weight: 600; letter-spacing: .08em; text-transform: uppercase; color: var(--vp-c-text-3); }
.ew-foot__col ul { list-style: none; margin: 0; padding: 0; }
.ew-foot__col li { font-size: 14px; line-height: 1.9; color: var(--vp-c-text-2); }
.ew-foot__col a { color: var(--vp-c-text-2); }
.ew-foot__col a:hover { color: var(--vp-c-brand-1); }
.ew-foot__grid { display: grid; grid-template-columns: 1fr 1fr; column-gap: 16px; }
@media (max-width: 959px) {
  .ew-foot__inner { grid-template-columns: 1fr; }
  .ew-foot__cols { grid-template-columns: 1fr; }
}
@media (min-width: 640px) { .ew-foot { padding: 48px 48px 8px; } }
@media (min-width: 960px) { .ew-foot { padding: 56px 64px 8px; } }
</style>
