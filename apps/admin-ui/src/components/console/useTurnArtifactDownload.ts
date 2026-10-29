/** 对话页 / 调试台每一轮产物按钮的下载动作(B-235)。
 *
 *  按这一轮登记的版本号下载:同一个用户的另一段会话之后再登记同名产物,也不会把这一轮的
 *  那一版换掉。那一版登记在逐版留存上线之前、又已不是最新版时,后端回 409 —— 这里如实
 *  提示「这一版内容没有单独保留」,并给出「下载最新版」让人自己决定,绝不悄悄换成最新版。
 *  其它失败照旧是一条带后端原因的错误提示(静默吞错会让「下载失败」看起来像「点了没反应」)。
 */
import { useCallback } from "react";
import { App } from "antd";
import { useTranslation } from "react-i18next";

import { ApiError, errMessage, type TenantScope } from "../../api/client";
import { downloadSessionArtifact } from "../../api/sessions";

export function useTurnArtifactDownload(
  threadId: string | null,
  tenantScope: TenantScope,
): (name: string, version?: number) => Promise<void> {
  const { message, modal } = App.useApp();
  const { t } = useTranslation();

  return useCallback(
    async (name: string, version?: number) => {
      if (threadId === null) return;
      const reportFailure = (err: unknown) => {
        message.error(t("artifacts_page.download_failed", { detail: errMessage(err) }));
      };
      try {
        await downloadSessionArtifact(threadId, name, tenantScope, version);
      } catch (err) {
        if (version !== undefined && err instanceof ApiError && err.status === 409) {
          modal.confirm({
            title: t("artifacts_page.version_not_kept_title", { name, version }),
            content: t("artifacts_page.version_not_kept_body"),
            okText: t("artifacts_page.download_latest"),
            cancelText: t("common.cancel"),
            onOk: async () => {
              try {
                await downloadSessionArtifact(threadId, name, tenantScope, undefined);
              } catch (latestErr) {
                reportFailure(latestErr);
              }
            },
          });
          return;
        }
        reportFailure(err);
      }
    },
    [threadId, tenantScope, message, modal, t],
  );
}
