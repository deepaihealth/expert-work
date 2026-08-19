/**
 * ConsoleShell — the debug console's grid shell (调试台重设计 PR-A Task 13;
 * PR-A.2 Task 6 made ``inspect`` optional). Renders ``sidebar`` / ``main`` /
 * ``inspect`` verbatim in a CSS grid (see ``console.css``); the <1200px
 * column-collapse is CSS-only — the left column shrinks to a 48px icon rail
 * and the always-mounted ``sidebar`` node in that column is hidden, replaced
 * by the same node re-rendered inside a Drawer the rail button opens.
 * Omitting ``inspect`` drops to a two-column layout (``.ew-console--two`` on
 * the root, no ``.ew-console__inspect`` node at all) for pages that don't
 * have a trajectory panel.
 *
 * The root element measures its own distance from the viewport top (mount +
 * window resize) and writes it into the ``--ew-console-top`` CSS variable,
 * so ``console.css`` can size the grid to fill exactly to the viewport
 * bottom instead of a hard-coded ``calc(100vh - 360px)`` offset that goes
 * stale whenever the chrome above the console changes height (调试台重设计
 * §八.1).
 */
import { type JSX, type ReactNode, useLayoutEffect, useRef, useState } from "react";
import { Drawer } from "antd";
import { PanelLeftOpen } from "lucide-react";

import "./console.css";

export interface ConsoleShellProps {
  sidebar: ReactNode;
  main: ReactNode;
  /** 右列(轨迹面板)。缺省 = 两栏形态:不渲染 ``.ew-console__inspect``,
   *  根节点加 ``ew-console--two`` 修饰类(grid 264px 1fr,<1200px 48px 1fr)。 */
  inspect?: ReactNode;
  sidebarLabel: string;
}

export function ConsoleShell({
  sidebar,
  main,
  inspect,
  sidebarLabel,
}: ConsoleShellProps): JSX.Element {
  const [railOpen, setRailOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);

  useLayoutEffect(() => {
    const el = rootRef.current;
    if (!el) return;
    const apply = () => {
      el.style.setProperty(
        "--ew-console-top",
        `${Math.max(0, Math.round(el.getBoundingClientRect().top))}px`,
      );
    };
    apply();
    window.addEventListener("resize", apply);
    return () => window.removeEventListener("resize", apply);
  }, []);

  const rootClassName =
    inspect === undefined ? "ew-console ew-console--two" : "ew-console";

  return (
    <div ref={rootRef} className={rootClassName} data-testid="playground-tab">
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
      {inspect !== undefined && (
        <div className="ew-console__inspect">{inspect}</div>
      )}
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
