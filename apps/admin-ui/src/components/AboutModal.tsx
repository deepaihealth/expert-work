import { Descriptions, Modal, Tag, Typography } from "antd";
import dayjs from "dayjs";
import { useTranslation } from "react-i18next";

import { readBuildInfo } from "../config/env";

const COMMIT_URL_BASE = "https://github.com/deepaihealth/expert-work/commit/";

interface AboutModalProps {
  open: boolean;
  onClose: () => void;
}

/** 「x 分钟前 / 3 天前」—— 跟着当前界面语言走。 */
function relativeTime(iso: string, locale: string): string | null {
  const then = dayjs(iso);
  if (!then.isValid()) return null;
  const diffS = Math.round((then.valueOf() - Date.now()) / 1000);
  const rtf = new Intl.RelativeTimeFormat(locale, { numeric: "auto" });
  const abs = Math.abs(diffS);
  if (abs < 60) return rtf.format(diffS, "second");
  if (abs < 3600) return rtf.format(Math.round(diffS / 60), "minute");
  if (abs < 86400) return rtf.format(Math.round(diffS / 3600), "hour");
  return rtf.format(Math.round(diffS / 86400), "day");
}

/** 线上跑的是哪个版本、哪个环境、什么时候发的。数据全来自构建时烤进去的
 *  三个值(config/env.ts readBuildInfo),零后端请求。 */
export function AboutModal({ open, onClose }: AboutModalProps) {
  const { t, i18n } = useTranslation();
  const { version, env, builtAt } = readBuildInfo();

  const envTag =
    env === "prod" ? (
      <Tag color="red">{t("about.env_prod")}</Tag>
    ) : env === "test" ? (
      <Tag color="blue">{t("about.env_test")}</Tag>
    ) : (
      <Tag>{t("about.env_local")}</Tag>
    );

  const built = builtAt !== undefined && dayjs(builtAt).isValid() ? dayjs(builtAt) : null;
  const relative = builtAt !== undefined ? relativeTime(builtAt, i18n.language) : null;

  return (
    <Modal open={open} onCancel={onClose} footer={null} title={t("about.title")} width={440}>
      <Descriptions column={1} size="small" colon={false} labelStyle={{ width: 96 }}>
        <Descriptions.Item label={t("about.version")}>
          {version !== undefined ? (
            <Typography.Link href={`${COMMIT_URL_BASE}${version}`} target="_blank" rel="noreferrer" code>
              {version}
            </Typography.Link>
          ) : (
            <Typography.Text type="secondary">{t("about.unknown")}</Typography.Text>
          )}
        </Descriptions.Item>
        <Descriptions.Item label={t("about.environment")}>{envTag}</Descriptions.Item>
        <Descriptions.Item label={t("about.last_updated")}>
          {built !== null ? (
            <span>
              {built.format("YYYY-MM-DD HH:mm")}
              {relative !== null && (
                <Typography.Text type="secondary" style={{ marginLeft: 8 }}>
                  {relative}
                </Typography.Text>
              )}
            </span>
          ) : (
            <Typography.Text type="secondary">{t("about.unknown")}</Typography.Text>
          )}
        </Descriptions.Item>
      </Descriptions>
    </Modal>
  );
}
