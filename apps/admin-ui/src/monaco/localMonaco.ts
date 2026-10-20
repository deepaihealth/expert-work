/**
 * Locally bundled Monaco (B-152). Loaded lazily by ``setup.ts``.
 *
 * Only what the console uses is included instead of ``editor.main``:
 *   - editor core (``editor`` + ``features/register.all``) + the editor worker
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
 *
 * Paths are the entry points monaco-editor >= 0.56 exports (its ``exports``
 * map sends ``monaco-editor/<x>`` to ``esm/vs/<x>.js``; the old
 * ``monaco-editor/esm/vs/...`` deep imports no longer resolve).
 */
import * as monaco from "monaco-editor/editor";
import "monaco-editor/features/register.all";
import "monaco-editor/languages/features/json/register";
import "monaco-editor/languages/features/typescript/register";
import "monaco-editor/languages/features/html/register";
import "monaco-editor/languages/features/css/register";
import "monaco-editor/languages/definitions/yaml/register";
import "monaco-editor/languages/definitions/markdown/register";
import "monaco-editor/languages/definitions/python/register";
import "monaco-editor/languages/definitions/javascript/register";
import "monaco-editor/languages/definitions/typescript/register";
import "monaco-editor/languages/definitions/html/register";
import "monaco-editor/languages/definitions/css/register";
import "monaco-editor/languages/definitions/shell/register";
import "monaco-editor/languages/definitions/sql/register";
import "monaco-editor/languages/definitions/ini/register";
import EditorWorker from "monaco-editor/editor/editor.worker?worker";
import JsonWorker from "monaco-editor/languages/features/json/json.worker?worker";
import TsWorker from "monaco-editor/languages/features/typescript/ts.worker?worker";
import HtmlWorker from "monaco-editor/languages/features/html/html.worker?worker";
import CssWorker from "monaco-editor/languages/features/css/css.worker?worker";

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
