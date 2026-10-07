// Vite ``?worker`` imports (tsconfig only loads @types/node, not vite/client).
declare module "*?worker" {
  const WorkerCtor: new () => Worker;
  export default WorkerCtor;
}

// Side-effect-only language contributions the package does not expose types for.
declare module "monaco-editor/esm/vs/basic-languages/*";
declare module "monaco-editor/esm/vs/language/json/monaco.contribution";

// edcore.main = editor.main minus the bundled languages; same public API.
declare module "monaco-editor/esm/vs/editor/edcore.main" {
  export * from "monaco-editor/esm/vs/editor/editor.api";
}
