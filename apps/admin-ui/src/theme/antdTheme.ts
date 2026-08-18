/**
 * expert_work Admin tokens → Antd 5 ConfigProvider theme 映射
 *
 * 思路:CSS custom properties 仍是 source of truth(`tokens.css`);
 * Antd 内部不读 CSS variable,所以这里给 Antd 一份硬编码 token 值。
 * dark / light 切换 = 同时切 html[data-theme] + Antd theme.algorithm。
 */
import { theme as antdTheme, type ThemeConfig } from "antd";

// 与 tokens.css 1:1 对齐(brand / accent / semantic)
export const EXPERT_WORK_COLORS = {
  brand: {
    50: "#EEF2FB",
    200: "#B4C4EE",
    300: "#8FA9E6",
    400: "#4E74D2",
    500: "#2C56C4", // primary(ink blue,2026-08-18 品牌重定)
    600: "#2146A6",
    700: "#1A3785",
  },
  accent: {
    400: "#c084fc",
    500: "#a855f7",
    700: "#7e22ce",
  },
  success: { 500: "#22c55e", 700: "#15803d" },
  warning: { 500: "#f59e0b", 700: "#b45309" },
  danger: { 500: "#ef4444", 700: "#b91c1c" },
} as const;

const BASE_TOKEN = {
  colorPrimary: EXPERT_WORK_COLORS.brand[500],
  colorSuccess: EXPERT_WORK_COLORS.success[500],
  colorWarning: EXPERT_WORK_COLORS.warning[500],
  colorError: EXPERT_WORK_COLORS.danger[500],
  colorInfo: EXPERT_WORK_COLORS.brand[500],

  borderRadius: 6,
  borderRadiusSM: 4,
  borderRadiusLG: 8,

  fontFamily:
    "Inter, -apple-system, BlinkMacSystemFont, 'PingFang SC', 'Microsoft YaHei', 'Noto Sans SC', sans-serif",
  fontFamilyCode:
    "'JetBrains Mono', 'SF Mono', Consolas, 'Liberation Mono', monospace",
  fontSize: 14,
  fontSizeSM: 13,

  controlHeight: 32,
  controlHeightSM: 28,
  controlHeightLG: 40,

  motionDurationFast: "0.1s",
  motionDurationMid: "0.15s",
  motionDurationSlow: "0.2s",

  wireframe: false,
};

export const darkTheme: ThemeConfig = {
  algorithm: antdTheme.darkAlgorithm,
  token: {
    ...BASE_TOKEN,
    colorBgBase: "#0a0b0f", // tokens.css --ew-surface-bg
    colorBgContainer: "#161921", // --ew-surface-base
    colorBgElevated: "#232730", // --ew-surface-raised
    colorBgLayout: "#0a0b0f",
    colorBorder: "#3f4452", // --ew-border-default
    colorBorderSecondary: "#232730", // --ew-border-subtle
    colorText: "#f4f5f7",
    colorTextSecondary: "#a0a4ae",
    colorTextTertiary: "#74798a",
    colorTextQuaternary: "#5a5f70",
    colorLink: EXPERT_WORK_COLORS.brand[300],
    colorLinkHover: EXPERT_WORK_COLORS.brand[200],
  },
  components: {
    Layout: {
      bodyBg: "#0a0b0f",
      siderBg: "#161921",
      headerBg: "#161921",
      headerHeight: 48,
      headerPadding: "0 24px",
    },
    Menu: {
      itemBg: "transparent",
      itemSelectedBg: "rgba(6, 182, 212, 0.12)",
      itemSelectedColor: "#f4f5f7",
      itemHoverBg: "rgba(255, 255, 255, 0.04)",
      itemHeight: 32,
    },
    Table: {
      headerBg: "#232730",
      headerColor: "#74798a",
      rowHoverBg: "rgba(255, 255, 255, 0.04)",
    },
    Tabs: {
      itemSelectedColor: "#f4f5f7",
      inkBarColor: EXPERT_WORK_COLORS.brand[500],
    },
    Button: {
      fontWeight: 500,
    },
    Card: {
      headerBg: "transparent",
    },
  },
};

export const lightTheme: ThemeConfig = {
  algorithm: antdTheme.defaultAlgorithm,
  token: {
    ...BASE_TOKEN,
    colorPrimary: EXPERT_WORK_COLORS.brand[500],
    colorBgBase: "#fafbfc",
    colorBgContainer: "#ffffff",
    colorBgElevated: "#ffffff",
    colorBgLayout: "#fafbfc",
    colorBorder: "#cacdd4",
    colorBorderSecondary: "#e5e7eb",
    colorText: "#161921",
    colorTextSecondary: "#5a5f70",
    colorTextTertiary: "#74798a",
    colorTextQuaternary: "#a0a4ae",
    colorLink: EXPERT_WORK_COLORS.brand[700],
    colorLinkHover: EXPERT_WORK_COLORS.brand[600],
  },
  components: {
    Layout: {
      bodyBg: "#fafbfc",
      siderBg: "#ffffff",
      headerBg: "#ffffff",
      headerHeight: 48,
      headerPadding: "0 24px",
    },
    Menu: {
      itemBg: "transparent",
      itemSelectedBg: "rgba(6, 182, 212, 0.08)",
      itemSelectedColor: "#161921",
      itemHoverBg: "rgba(0, 0, 0, 0.04)",
      itemHeight: 32,
    },
    Table: {
      headerBg: "#fafbfc",
      headerColor: "#74798a",
      rowHoverBg: "rgba(0, 0, 0, 0.04)",
    },
    Tabs: {
      itemSelectedColor: "#161921",
      inkBarColor: EXPERT_WORK_COLORS.brand[500],
    },
    Button: {
      fontWeight: 500,
    },
    Card: {
      headerBg: "transparent",
    },
  },
};
