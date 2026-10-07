/**
 * Locally bundled Monaco (B-152). Loaded lazily by ``setup.ts``.
 *
 * Only what the console uses is included instead of ``editor.main``:
 *   - editor core (``edcore.main``) + the editor worker
 *   - JSON language service (validation / hover) + its worker
 *   - Monarch tokenizers for the other languages the editors ask for:
 *     yaml, markdown, python, javascript, typescript, html, css, shell, sql, ini
 *     (the JS/TS/HTML/CSS language-service workers are deliberately left out;
 *     they are multi-MB and the console never edits those with diagnostics).
 * Keep in sync with ``languageFor`` in pages/skill_detail/FileEditor.tsx.
 *
 * Workers are Vite ``?worker`` bundles, emitted as same-origin files, so no
 * CSP / blob: allowance is needed.
 */
import * as monaco from "monaco-editor/esm/vs/editor/edcore.main";
import "monaco-editor/esm/vs/language/json/monaco.contribution";
import "monaco-editor/esm/vs/basic-languages/yaml/yaml.contribution";
import "monaco-editor/esm/vs/basic-languages/markdown/markdown.contribution";
import "monaco-editor/esm/vs/basic-languages/python/python.contribution";
import "monaco-editor/esm/vs/basic-languages/javascript/javascript.contribution";
import "monaco-editor/esm/vs/basic-languages/typescript/typescript.contribution";
import "monaco-editor/esm/vs/basic-languages/html/html.contribution";
import "monaco-editor/esm/vs/basic-languages/css/css.contribution";
import "monaco-editor/esm/vs/basic-languages/shell/shell.contribution";
import "monaco-editor/esm/vs/basic-languages/sql/sql.contribution";
import "monaco-editor/esm/vs/basic-languages/ini/ini.contribution";
import EditorWorker from "monaco-editor/esm/vs/editor/editor.worker?worker";
import JsonWorker from "monaco-editor/esm/vs/language/json/json.worker?worker";

self.MonacoEnvironment = {
  getWorker(_workerId: string, label: string): Worker {
    return label === "json" ? new JsonWorker() : new EditorWorker();
  },
};

export default monaco;
