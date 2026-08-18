<script setup lang="ts">
// 能力卡区的小标题(默认主题的 features 没有标题位)。
</script>

<template>
  <div class="ew-fhead">
    <div class="ew-fhead__inner">
      <p class="ew-fhead__eyebrow">文档章节</p>
      <h2 class="ew-fhead__title">按你手头的任务找章节</h2>
      <p class="ew-fhead__lead">每一章都按「你要做什么」组织：接口在哪、参数是什么、返回怎么处理、出错怎么办。</p>
    </div>
  </div>
</template>

<style scoped>
.ew-fhead { padding: 40px 24px 0; }
.ew-fhead__inner { max-width: 1152px; margin: 0 auto; }
.ew-fhead__eyebrow { margin: 0 0 8px; font-size: 12px; font-weight: 600; letter-spacing: .12em; text-transform: uppercase; color: var(--vp-c-brand-1); }
.ew-fhead__title { margin: 0; font-size: 26px; font-weight: 700; letter-spacing: -.01em; line-height: 1.3; color: var(--vp-c-text-1); }
.ew-fhead__lead { margin: 12px 0 8px; max-width: 640px; font-size: 15px; line-height: 1.7; color: var(--vp-c-text-2); }
@media (max-width: 959px) { .ew-fhead__title { font-size: 22px; } }
@media (min-width: 640px) { .ew-fhead { padding: 40px 48px 0; } }
@media (min-width: 960px) { .ew-fhead { padding: 48px 64px 0; } }
</style>
