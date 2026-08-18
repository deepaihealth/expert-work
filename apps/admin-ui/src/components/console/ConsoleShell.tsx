/**
 * ConsoleShell — the debug console's three-column grid shell (调试台重设计
 * PR-A Task 13). Renders ``sidebar`` / ``main`` / ``inspect`` verbatim in
 * a CSS grid (see ``console.css``); the <1200px column-collapse is
 * CSS-only — the left column shrinks to a 48px icon rail and the
 * always-mounted ``sidebar`` node in that column is hidden, replaced by
 * the same node re-rendered inside a Drawer the rail button opens.
 *
 * ``CONSOLE_HEIGHT_OFFSET_PX`` mirrors the ``calc(100vh - 360px)`` this
 * replaces from ``PlaygroundTab.tsx`` — exported so other height math in
 * this feature stays in sync with the one number instead of repeating
 * the literal.
 */
import { type JSX, type ReactNode, useState } from "react";
import { Drawer } from "antd";
import { PanelLeftOpen } from "lucide-react";

import "./console.css";

export const CONSOLE_HEIGHT_OFFSET_PX = 360;

export interface ConsoleShellProps {
  sidebar: ReactNode;
  main: ReactNode;
  inspect: ReactNode;
  sidebarLabel: string;
}

export function ConsoleShell({
  sidebar,
  main,
  inspect,
  sidebarLabel,
}: ConsoleShellProps): JSX.Element {
  const [railOpen, setRailOpen] = useState(false);

  return (
    <div className="ew-console" data-testid="playground-tab">
      <div className="ew-console__sidebar">
        <button
          type="button"
          className="ew-console__rail"
          data-testid="console-sidebar-rail-open"
          aria-label={sidebarLabel}
          onClick={() => setRailOpen(true)}
        >
          <PanelLeftOpen size={18} strokeWidth={1.5} />
        </button>
        <div className="ew-console__sidebar-body">{sidebar}</div>
      </div>
      <div className="ew-console__main">{main}</div>
      <div className="ew-console__inspect">{inspect}</div>
      <Drawer
        open={railOpen}
        onClose={() => setRailOpen(false)}
        placement="left"
        width={320}
        destroyOnHidden
        title={sidebarLabel}
      >
        {sidebar}
      </Drawer>
    </div>
  );
}
