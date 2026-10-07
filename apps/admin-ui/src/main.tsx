import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";

import App from "./App";
import { AuthProvider } from "./auth/AuthContext";
import "./i18n";
import { configureMonacoLoader } from "./monaco/setup";
import { TenantScopeProvider } from "./tenant/TenantScopeContext";
import { ThemeProvider } from "./theme/ThemeContext";
import "./theme/tokens.css";
import "./theme/global.css";

configureMonacoLoader();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ThemeProvider>
      <BrowserRouter>
        <AuthProvider>
          <TenantScopeProvider>
            <App />
          </TenantScopeProvider>
        </AuthProvider>
      </BrowserRouter>
    </ThemeProvider>
  </StrictMode>,
);
