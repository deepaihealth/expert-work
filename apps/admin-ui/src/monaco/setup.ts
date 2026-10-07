/**
 * Serve Monaco from our own bundle instead of cdn.jsdelivr.net (B-152).
 *
 * ``@monaco-editor/loader`` (node_modules/@monaco-editor/loader/lib/es/loader/index.js)
 * only skips its CDN ``<script>`` path when ``config({ monaco })`` was given a
 * truthy value (lines 52-58: ``state.resolve(state.monaco)``). Passing the
 * imported module directly would pull ~3 MB into the entry chunk, so we pass a
 * lazy thenable instead: ``resolve(thenable)`` adopts it, which means the
 * ``import()`` below runs only when the first editor mounts. Every consumer in
 * ``@monaco-editor/react`` goes through ``loader.init().then(...)``, so it
 * never sees the thenable itself.
 */
import loader from "@monaco-editor/loader";

type LocalMonaco = typeof import("./localMonaco").default;

export function configureMonacoLoader(): void {
  const lazyMonaco = {
    then(
      onFulfilled: (m: LocalMonaco) => void,
      onRejected: (e: unknown) => void,
    ): void {
      import("./localMonaco").then((mod) => onFulfilled(mod.default), onRejected);
    },
  };
  loader.config({ monaco: lazyMonaco as unknown as LocalMonaco });
}
