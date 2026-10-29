/**
 * Switching a passage on or off from the console — B-175 P3-PR3. Shared by
 * the passage list and the review panel: one request, one message, the
 * refusals mapped (a busy document, a passage gone meanwhile).
 */
import { useCallback, useState } from "react";
import { App } from "antd";
import { useTranslation } from "react-i18next";

import { updatePassage, type Passage } from "../../../api/knowledge_maintenance";
import { passageWriteError } from "./passageErrors";

export interface PassageSwitch {
  /** The id of the passage whose switch is in flight. */
  switching: string | null;
  /** The passage as switched, or ``null`` when the switch was refused. */
  switchPassage: (passage: Passage, enabled: boolean) => Promise<Passage | null>;
}

export function usePassageSwitch(
  baseName: string,
  documentId: string,
  onGone: () => void,
): PassageSwitch {
  const { t } = useTranslation();
  const { message } = App.useApp();
  const [switching, setSwitching] = useState<string | null>(null);

  const switchPassage = useCallback(
    async (passage: Passage, enabled: boolean): Promise<Passage | null> => {
      setSwitching(passage.id);
      try {
        const switched = await updatePassage(baseName, documentId, passage.id, { enabled });
        message.success(t(enabled ? "kb_maintenance.toggled_on" : "kb_maintenance.toggled_off"));
        return switched;
      } catch (err) {
        const failure = passageWriteError(t, err);
        if (failure.kind === "message") {
          message.error(failure.text);
        } else {
          message.warning(t("kb_maintenance.error_gone"));
          onGone();
        }
        return null;
      } finally {
        setSwitching(null);
      }
    },
    [baseName, documentId, message, onGone, t],
  );

  return { switching, switchPassage };
}
