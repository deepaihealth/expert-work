<script setup lang="ts">
import { withBase } from 'vitepress'
// 「三步接入」—— 首页 hero 之下、能力卡之上。顺序即接入顺序,编号有意义。
const steps = [
  {
    n: '01',
    title: '取得 API Key',
    body: '由租户管理员在管理控制台创建服务账号并发放。调用时放在 Authorization: Bearer 请求头里；一把 write 档位的 key 就够对接。',
    link: '/guide/quickstart#_1-1-取得-api-key',
    linkText: '怎么拿到 key',
  },
  {
    n: '02',
    title: '发起对话',
    body: 'POST /v1/agents/{agent_code}/runs，带上 user_id 与 input。stream 模式的响应体就是事件流；queue 模式后台执行、稍后查询结果。',
    link: '/guide/chat#_2-2-发起对话',
    linkText: '看请求参数',
  },
  {
    n: '03',
    title: '处理事件流',
    body: '按事件名分发：token 做打字机预览，updates 重建对话内容与工具调用，end 收尾。断线后带 since_seq 续传，不丢事件。',
    link: '/guide/sse-events#_3-4-每个事件怎么处理',
    linkText: '每个事件怎么处理',
  },
]
</script>

<template>
  <section class="ew-steps">
    <div class="ew-steps__inner">
      <header class="ew-steps__head">
        <p class="ew-steps__eyebrow">接入流程</p>
        <h2 class="ew-steps__title">三步跑通，一天内可上线</h2>
        <p class="ew-steps__lead">
          对外只有一套 REST 接口和一条 SSE 事件流，没有 SDK 依赖。上线前按
          <a :href="withBase('/guide/best-practices')">对接注意事项与自测清单</a> 过一遍即可。
        </p>
      </header>
      <ol class="ew-steps__list">
        <li v-for="s in steps" :key="s.n" class="ew-step">
          <span class="ew-step__n">{{ s.n }}</span>
          <h3 class="ew-step__title">{{ s.title }}</h3>
          <p class="ew-step__body">{{ s.body }}</p>
          <a class="ew-step__link" :href="withBase(s.link)">{{ s.linkText }} <span aria-hidden="true">→</span></a>
        </li>
      </ol>
    </div>
  </section>
</template>

<style scoped>
.ew-steps { padding: 8px 24px 0; }
.ew-steps__inner { max-width: 1152px; margin: 0 auto; padding-top: 40px; border-top: 1px solid var(--vp-c-divider); }
.ew-steps__head { max-width: 640px; }
.ew-steps__eyebrow { margin: 0 0 8px; font-size: 12px; font-weight: 600; letter-spacing: .12em; text-transform: uppercase; color: var(--vp-c-brand-1); }
.ew-steps__title { margin: 0; font-size: 26px; font-weight: 700; letter-spacing: -.01em; line-height: 1.3; color: var(--vp-c-text-1); text-wrap: balance; }
.ew-steps__lead { margin: 12px 0 0; font-size: 15px; line-height: 1.7; color: var(--vp-c-text-2); }
.ew-steps__lead a { color: var(--vp-c-brand-1); text-decoration: underline; text-underline-offset: 3px; }
.ew-steps__list { list-style: none; margin: 28px 0 0; padding: 0; display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; }
.ew-step { position: relative; padding: 22px 22px 20px; border: 1px solid var(--vp-c-divider); border-radius: 12px; background: var(--ew-card-bg); }
.ew-step__n { display: inline-block; font-family: var(--vp-font-family-mono); font-size: 12px; font-weight: 600; letter-spacing: .06em; color: var(--vp-c-brand-1); }
.ew-step__title { margin: 6px 0 8px; font-size: 17px; font-weight: 650; color: var(--vp-c-text-1); }
.ew-step__body { margin: 0; font-size: 14px; line-height: 1.7; color: var(--vp-c-text-2); }
.ew-step__link { display: inline-block; margin-top: 12px; font-size: 14px; font-weight: 500; color: var(--vp-c-brand-1); }
.ew-step__link:hover { color: var(--vp-c-brand-2); }
@media (max-width: 959px) { .ew-steps__list { grid-template-columns: 1fr; } .ew-steps__title { font-size: 22px; } }
@media (min-width: 640px) { .ew-steps { padding: 8px 48px 0; } }
@media (min-width: 960px) { .ew-steps { padding: 8px 64px 0; } }
</style>
