// B-315 — the minimal "this form can't be saved yet" channel. A widget that
// holds edits it refuses to write back (e.g. OutputSchemaEditor while a
// field name is duplicated / blank / invalid) reports that through
// ``useReportFormErrors``; ``ManifestEditor`` provides the context, folds
// every report into one ``hasErrors`` and hands it to its host
// (``onValidityChange``), which disables / blocks its save action.
// Outside a provider the hook is a no-op.
import { createContext, useContext, useEffect } from "react";

/** ``(source, hasErrors)`` — one entry per reporting widget. */
export type ReportFormErrors = (source: string, hasErrors: boolean) => void;

export const FormErrorsContext = createContext<ReportFormErrors | null>(null);

/** Report ``hasErrors`` for ``source``; cleared when the widget unmounts. */
export function useReportFormErrors(source: string, hasErrors: boolean): void {
  const report = useContext(FormErrorsContext);
  useEffect(() => {
    report?.(source, hasErrors);
  }, [report, source, hasErrors]);
  useEffect(() => () => report?.(source, false), [report, source]);
}
