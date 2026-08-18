<script setup lang="ts">
import DefaultTheme from 'vitepress/theme'
import HeroCodeCard from './components/HeroCodeCard.vue'
import HomeSteps from './components/HomeSteps.vue'
import HomeFooter from './components/HomeFooter.vue'
import HomeFeaturesHead from './components/HomeFeaturesHead.vue'

const { Layout } = DefaultTheme
</script>

<template>
  <Layout>
    <template #home-hero-image><HeroCodeCard /></template>
    <template #home-hero-after><HomeSteps /></template>
    <template #home-features-before><HomeFeaturesHead /></template>
    <template #home-features-after><HomeFooter /></template>
  </Layout>
</template>
