import type { Theme } from 'vitepress'
import DefaultTheme from 'vitepress/theme'
import Layout from './Layout.vue'
import './custom.css'

// 默认主题 + 首页插槽(hero 代码卡 / 三步接入 / 页脚),正文页不受影响。
export default {
  extends: DefaultTheme,
  Layout,
} satisfies Theme
