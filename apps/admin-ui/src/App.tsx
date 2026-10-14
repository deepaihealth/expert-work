import { ConfigProvider, App as AntApp } from "antd";
import antdEn from "antd/locale/en_US";
import antdZhCN from "antd/locale/zh_CN";
import { useTranslation } from "react-i18next";
import { Route, Routes } from "react-router-dom";

import { useTheme } from "./theme/ThemeContext";
import { darkTheme, lightTheme } from "./theme/antdTheme";
import { Shell } from "./components/Shell";
import { CommandPaletteProvider } from "./components/CommandPalette";
import { AppRouter } from "./router";
import { ProtectedRoute } from "./auth/ProtectedRoute";
import { SetupGate } from "./auth/SetupGate";
import { AuthCallback } from "./pages/AuthCallback";
import { AuthSilent } from "./pages/AuthSilent";
import { Login } from "./pages/Login";
import { SetupWizard } from "./pages/SetupWizard";
import { TenantScopeGate } from "./tenant/TenantScopeContext";

export default function App() {
  const { mode } = useTheme();
  const themeConfig = mode === "dark" ? darkTheme : lightTheme;
  // antd's built-in strings (pagination "50 / page", empty states, modal
  // buttons…) follow the i18n language — useTranslation re-renders on switch.
  const { i18n } = useTranslation();
  const antdLocale = i18n.language === "en" ? antdEn : antdZhCN;

  return (
    <ConfigProvider theme={themeConfig} componentSize="middle" locale={antdLocale}>
      <AntApp>
        {/* SetupGate probes /v1/setup/status and steers an
            un-initialized platform to /setup *before* ProtectedRoute can
            kick off an OIDC redirect (no account exists yet to log in
            with). */}
        <SetupGate>
          <Routes>
            <Route path="/setup" element={<SetupWizard />} />
            <Route path="/login" element={<Login />} />
            {/* OIDC callback + silent renew routes must stay outside
                ProtectedRoute — the user is unauthenticated at the moment
                the IdP redirects them here. */}
            <Route path="/auth/callback" element={<AuthCallback />} />
            <Route path="/auth/silent" element={<AuthSilent />} />
            <Route
              path="/*"
              element={
                <ProtectedRoute>
                  <CommandPaletteProvider>
                    <Shell>
                      {/* Pages mount once the tenant scope has settled —
                          never on home first after a switched-in reload
                          (B-183). */}
                      <TenantScopeGate>
                        <AppRouter />
                      </TenantScopeGate>
                    </Shell>
                  </CommandPaletteProvider>
                </ProtectedRoute>
              }
            />
          </Routes>
        </SetupGate>
      </AntApp>
    </ConfigProvider>
  );
}
