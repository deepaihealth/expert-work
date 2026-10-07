/**
 * Locally bundled Monaco (B-152). Loaded lazily by ``setup.ts``.
 *
 * Only what the console uses is included instead of ``editor.main``:
 *   - editor core (``edcore.main``) + the editor worker
 *   - JSON language service (validation / hover) + its worker
 *   - JS/TS, HTML and CSS language services (diagnostics / completion) + their
 *     workers. Each worker is its own file (ts ~7 MB, css ~1 MB, html ~0.7 MB)
 *     and is only fetched when a model of that language is opened — opening a
 *     YAML / Markdown / Python file downloads none of them (e2e pins this).
 *   - Monarch tokenizers for the other languages the editors ask for:
 *     yaml, markdown, python, shell, sql, ini
 * Keep in sync with ``languageFor`` in pages/skill_detail/FileEditor.tsx.
 *
 * Workers are Vite ``?worker`` bundles, emitted as same-origin files, so no
 * CSP / blob: allowance is needed.
 */
import * as monaco from "monaco-editor/esm/vs/editor/edcore.main";
import "monaco-editor/esm/vs/language/json/monaco.contribution";
import "monaco-editor/esm/vs/language/typescript/monaco.contribution";
import "monaco-editor/esm/vs/language/html/monaco.contribution";
import "monaco-editor/esm/vs/language/css/monaco.contribution";
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
import TsWorker from "monaco-editor/esm/vs/language/typescript/ts.worker?worker";
import HtmlWorker from "monaco-editor/esm/vs/language/html/html.worker?worker";
import CssWorker from "monaco-editor/esm/vs/language/css/css.worker?worker";

self.MonacoEnvironment = {
  getWorker(_workerId: string, label: string): Worker {
    if (label === "json") return new JsonWorker();
    if (label === "typescript" || label === "javascript") return new TsWorker();
    if (label === "html" || label === "handlebars" || label === "razor") return new HtmlWorker();
    if (label === "css" || label === "scss" || label === "less") return new CssWorker();
    return new EditorWorker();
  },
};

export default monaco;
