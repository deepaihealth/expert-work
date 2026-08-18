<script setup lang="ts">
// 首页 hero 右侧:「第一次调用」—— 一条 curl,响应体就是事件流。
// 内容与第 1 章 1.2 / 1.3 一致;这里只做示意,不承担字段定义(字段以第 3 章为准)。
</script>

<template>
  <div class="ew-hero-card" aria-label="第一次调用示例">
    <div class="ew-hero-card__bar">
      <span class="ew-hero-card__dots"><i /><i /><i /></span>
      <span class="ew-hero-card__title">第一次调用 · 响应即事件流</span>
      <span class="ew-hero-card__live"><i />stream</span>
    </div>
    <pre class="ew-hero-card__body"><code><span class="c-prompt">$</span> <span class="c-cmd">curl</span> -N -X POST <span class="c-url">https://&lt;your-domain&gt;/v1/agents/{agent_code}/runs</span> \
    -H <span class="c-str">"Authorization: Bearer &lt;key&gt;"</span> \
    -d <span class="c-str">'{"user_id":"u-123","input":"你好","mode":"stream"}'</span>

<span class="c-ev">event:</span> metadata
<span class="c-data">data:</span> {"run_id":"7c1e…","thread_id":"9f2c…"}

<span class="c-ev">event:</span> token
<span class="c-data">data:</span> {"step":1,"channel":"content","text":"你好"}

<span class="c-ev">event:</span> updates
<span class="c-data">data:</span> {"agent":{"messages":[…],"step_count":1}}

<span class="c-ev">event:</span> end
<span class="c-data">data:</span> {"status":"success"}</code></pre>
  </div>
</template>

<style scoped>
.ew-hero-card {
  width: 100%;
  max-width: 560px;
  border: 1px solid var(--vp-c-divider);
  border-radius: 14px;
  background: var(--ew-card-bg);
  box-shadow: var(--ew-card-shadow);
  overflow: hidden;
  text-align: left;
  font-family: var(--vp-font-family-mono);
}
.ew-hero-card__bar {
  display: flex; align-items: center; gap: 12px;
  padding: 10px 14px;
  border-bottom: 1px solid var(--vp-c-divider);
  background: var(--ew-card-bar-bg);
  font-family: var(--vp-font-family-base);
  font-size: 12px; color: var(--vp-c-text-2);
}
.ew-hero-card__dots { display: inline-flex; gap: 6px; }
.ew-hero-card__dots i { width: 10px; height: 10px; border-radius: 50%; background: var(--vp-c-divider); display: block; }
.ew-hero-card__title { flex: 1; letter-spacing: .01em; }
.ew-hero-card__live { display: inline-flex; align-items: center; gap: 6px; font-family: var(--vp-font-family-mono); font-size: 11px; color: var(--vp-c-text-2); }
.ew-hero-card__live i { width: 7px; height: 7px; border-radius: 50%; background: var(--ew-signal); box-shadow: 0 0 0 3px var(--ew-signal-soft); display: block; }
.ew-hero-card__body {
  margin: 0; padding: 16px 18px 18px;
  font-size: 12.5px; line-height: 1.7;
  color: var(--vp-c-text-1);
  overflow-x: auto;
  white-space: pre;
}
.c-prompt { color: var(--vp-c-text-3); }
.c-cmd { color: var(--vp-c-brand-1); font-weight: 600; }
.c-url { color: var(--vp-c-text-1); }
.c-str { color: var(--ew-code-str); }
.c-ev { color: var(--vp-c-brand-1); font-weight: 600; }
.c-data { color: var(--vp-c-text-3); }
@media (max-width: 959px) {
  .ew-hero-card { margin: 0 auto; }
  .ew-hero-card__body { font-size: 12px; }
}
</style>
