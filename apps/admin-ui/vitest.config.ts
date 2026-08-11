import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    css: false,
    // Vitest's default include picks up any *.test.* under the project
    // root — but Playwright owns ``e2e/``. Excluding it here keeps the
    // two runners cleanly separated.
    exclude: ["e2e/**", "node_modules/**", "dist/**", "storybook-static/**", "docs-site/**"],
  },
});
