/**
 * InspectPanel — the debug console's right-rail tab container (调试台重设计
 * PR-A Task 13). A controlled ``Segmented`` switches between the
 * trajectory and workspace panels; only the active tab's node is
 * mounted (the trajectory panel owns its own internal scroll — it must
 * not keep rendering, and scrolling, off-screen).
 */
import type { JSX, ReactNode } from "react";
import { Segmented } from "antd";
import { useTranslation } from "react-i18next";

export type InspectTab = "trajectory" | "workspace";

export interface InspectPanelProps {
  tab: InspectTab;
  onTabChange: (t: InspectTab) => void;
  trajectory: ReactNode;
  workspace: ReactNode;
}

export function InspectPanel({
  tab,
  onTabChange,
  trajectory,
  workspace,
}: InspectPanelProps): JSX.Element {
  const { t } = useTranslation();

  return (
    <>
      <Segmented
        value={tab}
        onChange={(value) => onTabChange(value as InspectTab)}
        options={[
          {
            value: "trajectory",
            label: (
              <span data-testid="console-inspect-tab-trajectory">
                {t("console.inspect_trajectory")}
              </span>
            ),
          },
          {
            value: "workspace",
            label: (
              <span data-testid="console-inspect-tab-workspace">
                {t("console.inspect_workspace")}
              </span>
            ),
          },
        ]}
      />
      <div style={{ flex: 1, minHeight: 0, overflow: "hidden" }}>
        {tab === "trajectory" ? trajectory : workspace}
      </div>
    </>
  );
}
