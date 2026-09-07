import { Button, Dropdown, Tooltip } from "antd";
import { Bell, Info, LogOut, Moon, Search, Sun, UserCircle2 } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { useAuth } from "../auth/AuthContext";
import { useTheme } from "../theme/ThemeContext";
import { TenantSwitcher } from "./TenantSwitcher";
import { useCommandPalette } from "./CommandPalette";
import { LanguageSwitcher } from "./LanguageSwitcher";
import { AboutModal } from "./AboutModal";

export function Topbar() {
  const { t } = useTranslation();
  const { mode, toggle } = useTheme();
  const { open } = useCommandPalette();
  const { identity, logout } = useAuth();
  const [aboutOpen, setAboutOpen] = useState(false);

  return (
    <>
      <TenantSwitcher />

      <button
        type="button"
        onClick={open}
        style={{
          display: "inline-flex",
          alignItems: "center",
          gap: 8,
          padding: "4px 12px",
          border: "1px solid var(--ew-border-subtle)",
          borderRadius: 6,
          background: "var(--ew-surface-raised)",
          color: "var(--ew-text-tertiary)",
          fontSize: 13,
          minWidth: 240,
          maxWidth: 320,
          cursor: "pointer",
        }}
      >
        <Search size={14} strokeWidth={1.5} />
        <span>{t("common.search_or_jump")}</span>
        <span style={{ flex: 1 }} />
        <span className="ew-kbd">⌘K</span>
      </button>

      <div style={{ flex: 1 }} />

      <LanguageSwitcher />

      <Tooltip title={mode === "dark" ? t("theme.switch_to_light") : t("theme.switch_to_dark")}>
        <Button type="text" size="small" onClick={toggle} icon={
          mode === "dark" ? <Sun size={16} strokeWidth={1.5} /> : <Moon size={16} strokeWidth={1.5} />
        } aria-label={t("theme.toggle")} />
      </Tooltip>

      <Tooltip title={t("common.notifications")}>
        <Button type="text" size="small" icon={<Bell size={16} strokeWidth={1.5} />} aria-label={t("common.notifications")} />
      </Tooltip>

      <Dropdown
        menu={{
          items: [
            {
              key: "identity",
              disabled: true,
              label: (
                <span style={{ fontSize: 12, color: "var(--ew-text-tertiary)" }}>
                  {identity?.displayName ?? t("common.anonymous")}
                  {identity?.isSystemAdmin && (
                    <span
                      style={{
                        marginLeft: 8,
                        padding: "1px 6px",
                        background: "var(--ew-accent-violet, #a855f7)",
                        color: "white",
                        borderRadius: 4,
                        fontSize: 10,
                      }}
                    >
                      sys
                    </span>
                  )}
                </span>
              ),
            },
            { type: "divider" },
            {
              key: "about",
              label: t("common.about"),
              icon: <Info size={14} strokeWidth={1.5} />,
              onClick: () => setAboutOpen(true),
            },
            {
              key: "logout",
              label: t("common.sign_out"),
              icon: <LogOut size={14} strokeWidth={1.5} />,
              onClick: logout,
            },
          ],
        }}
        trigger={["click"]}
        placement="bottomRight"
      >
        <Tooltip title={identity?.displayName ?? t("common.user_menu")}>
          <Button
            type="text"
            size="small"
            icon={<UserCircle2 size={16} strokeWidth={1.5} />}
            aria-label={t("common.user_menu")}
            data-testid="user-menu"
          />
        </Tooltip>
      </Dropdown>

      <AboutModal open={aboutOpen} onClose={() => setAboutOpen(false)} />
    </>
  );
}
