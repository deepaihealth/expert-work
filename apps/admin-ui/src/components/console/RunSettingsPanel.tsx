/**
 * RunSettingsPanel — 调试台右侧「运行设置」侧栏(调试台侧栏重设计,规格 A/B)。
 *
 * 主区(对话流 + 输入框)满高在左,本面板固定 380px 在右,承载低频内容:
 * 系统提示词卡(``SystemPromptCard``,点开只读 Drawer 看全文)与变量表单
 * (``VariablesForm``,父级作为 children 传入)。可收起成细 rail(带展开
 * 按钮 + 必填徽标);收起状态 localStorage 按 agent 记忆
 * (``expert_work.console.settingsCollapsed.<agent>``,PlanCard 同款约定)。
 *
 * 展开/收起的三条自动规则住在 ``useRunSettingsCollapse``:
 * 1. 必填变量未填(0→N 转变)→ 自动展开;手动收起后 missing 不变不再弹开
 *    (``VariablesForm`` 外层折叠同款语义)。
 * 2. 首次 run 成功发出 → 自动收起**一次**;用户手动展开过则永不自动收。
 * 3. 手动展开/收起写 localStorage;<1200px 由 CSS 隐藏本面板,内容改走
 *    页头触发的 Drawer(``PlaygroundTab`` 组装,ConsoleShell 左栏同款形态)。
 */
import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type JSX,
  type ReactNode,
} from "react";
import { Badge, Drawer, Typography } from "antd";
import { ChevronRight, FileText, PanelRightClose, PanelRightOpen } from "lucide-react";
import { useTranslation } from "react-i18next";

import { SystemPromptPanel } from "./RowDetailSystem";
import "./run_settings.css";

const { Text } = Typography;

const COLLAPSED_KEY_PREFIX = "expert_work.console.settingsCollapsed.";

function readStoredCollapsed(agentCode: string): boolean {
  if (typeof window === "undefined") return false;
  try {
    return window.localStorage.getItem(`${COLLAPSED_KEY_PREFIX}${agentCode}`) === "1";
  } catch {
    return false;
  }
}

function writeStoredCollapsed(agentCode: string, collapsed: boolean): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(
      `${COLLAPSED_KEY_PREFIX}${agentCode}`,
      collapsed ? "1" : "0",
    );
  } catch {
    // 存储失败只是丢记忆,不影响本次会话的展开/收起。
  }
}

export interface RunSettingsCollapse {
  collapsed: boolean;
  /** 手动展开(rail 按钮 / 之后不再自动收)。 */
  expand: () => void;
  /** 手动收起(面板头按钮)。 */
  collapse: () => void;
  /** 一轮 run 真正发出后调用:首次自动收起一次(仅一次)。 */
  notifyRunDispatched: () => void;
}

export function useRunSettingsCollapse(args: {
  agentCode: string;
  missingCount: number;
}): RunSettingsCollapse {
  const { agentCode, missingCount } = args;
  const [collapsed, setCollapsed] = useState<boolean>(
    () => missingCount === 0 && readStoredCollapsed(agentCode),
  );
  const userExpandedRef = useRef(false);
  const autoCollapsedRef = useRef(false);

  // agent 切换(本组件不重挂载)→ 一次性标记复位 + 读回该 agent 的记忆。
  useEffect(() => {
    userExpandedRef.current = false;
    autoCollapsedRef.current = false;
    setCollapsed(readStoredCollapsed(agentCode));
  }, [agentCode]);

  // 必填未填 → 自动展开。只在「无→有」的转变时强制:手动收起后 missing
  // 不变不再弹开。声明在上一个 effect 之后 —— agent 切换的同一个提交里,
  // 记忆态先落,缺必填再压上来。
  const hasMissing = missingCount > 0;
  useEffect(() => {
    if (hasMissing) setCollapsed(false);
  }, [hasMissing, agentCode]);

  const expand = useCallback(() => {
    userExpandedRef.current = true;
    setCollapsed(false);
    writeStoredCollapsed(agentCode, false);
  }, [agentCode]);

  const collapse = useCallback(() => {
    setCollapsed(true);
    writeStoredCollapsed(agentCode, true);
  }, [agentCode]);

  const notifyRunDispatched = useCallback(() => {
    if (autoCollapsedRef.current) return;
    // 「仅一次」:即便这次因用户手动展开而跳过,机会也用掉了。
    autoCollapsedRef.current = true;
    if (userExpandedRef.current) return;
    setCollapsed(true);
    writeStoredCollapsed(agentCode, true);
  }, [agentCode]);

  return { collapsed, expand, collapse, notifyRunDispatched };
}

/** 规格 B — 侧栏顶部的「系统提示词 · X 字」折叠卡,点开只读 Drawer 全文
 *  (等宽 + 保留换行,复用轨迹详情的 ``SystemPromptPanel`` 展示形态)。
 *  没配置提示词的 agent 不渲染。 */
export function SystemPromptCard({ template }: { template: string }): JSX.Element | null {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  if (template === "") return null;
  return (
    <>
      <button
        type="button"
        className="ew-run-settings__prompt-card"
        data-testid="console-settings-prompt-card"
        onClick={() => setOpen(true)}
      >
        <FileText
          size={14}
          strokeWidth={1.5}
          style={{ color: "var(--ew-text-secondary)", flexShrink: 0 }}
        />
        <Text type="secondary" style={{ fontSize: 12 }}>
          {t("console.settings_prompt_card", { count: template.length })}
        </Text>
        <ChevronRight
          size={14}
          strokeWidth={1.5}
          style={{ color: "var(--ew-text-tertiary)", marginLeft: "auto", flexShrink: 0 }}
        />
      </button>
      <Drawer
        open={open}
        onClose={() => setOpen(false)}
        placement="right"
        width={640}
        destroyOnHidden
        title={t("console.settings_prompt_drawer_title")}
      >
        <div data-testid="console-settings-prompt-drawer">
          <SystemPromptPanel text={template} />
        </div>
      </Drawer>
    </>
  );
}

export interface RunSettingsPanelProps {
  collapsed: boolean;
  onExpand: () => void;
  onCollapse: () => void;
  missingCount: number;
  children: ReactNode;
}

export function RunSettingsPanel({
  collapsed,
  onExpand,
  onCollapse,
  missingCount,
  children,
}: RunSettingsPanelProps): JSX.Element {
  const { t } = useTranslation();
  if (collapsed) {
    return (
      <aside
        className="ew-run-settings ew-run-settings--collapsed"
        data-testid="console-settings-panel"
        data-collapsed="true"
      >
        <span data-testid="console-settings-rail-badge">
          <Badge count={missingCount} size="small">
            <button
              type="button"
              className="ew-run-settings__rail"
              aria-label={t("console.settings_expand")}
              title={
                missingCount > 0
                  ? `${t("console.settings_expand")} · ${t("console.vars_missing_count", { count: missingCount })}`
                  : t("console.settings_expand")
              }
              data-testid="console-settings-expand"
              onClick={onExpand}
            >
              <PanelRightOpen size={18} strokeWidth={1.5} />
            </button>
          </Badge>
        </span>
      </aside>
    );
  }
  return (
    <aside
      className="ew-run-settings"
      data-testid="console-settings-panel"
      data-collapsed="false"
    >
      <div className="ew-run-settings__head">
        <Text strong style={{ fontSize: 13 }}>
          {t("console.settings_title")}
        </Text>
        {missingCount > 0 && (
          <Text type="danger" style={{ fontSize: 12 }} data-testid="console-settings-badge">
            {t("console.vars_missing_count", { count: missingCount })}
          </Text>
        )}
        <button
          type="button"
          className="ew-run-settings__collapse"
          aria-label={t("console.settings_collapse")}
          title={t("console.settings_collapse")}
          data-testid="console-settings-collapse"
          onClick={onCollapse}
        >
          <PanelRightClose size={16} strokeWidth={1.5} />
        </button>
      </div>
      <div className="ew-run-settings__body">{children}</div>
    </aside>
  );
}
