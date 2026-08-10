/**
 * One-time credential panel — member-password-provisioning Task 4.
 *
 * ``POST /v1/tenants`` echoes the first admin's freshly generated
 * ``initial_password`` exactly once, on the create response — the backend
 * never returns it again. This panel is the single place that surfaces it:
 * login URL / account / password, a "shown only once" warning, per-field
 * copy (antd ``Typography.Text copyable``), and a "copy all" button.
 *
 * No persistence: the password lives only in props for one render — never
 * written to localStorage, a store, or the console.
 */
import { Alert, App, Button, Space, Typography } from "antd";
import { useTranslation } from "react-i18next";

const { Text } = Typography;

export interface OneTimeCredentialPanelProps {
  account: string;
  password: string;
  loginUrl: string;
}

export function OneTimeCredentialPanel({
  account,
  password,
  loginUrl,
}: OneTimeCredentialPanelProps) {
  const { t } = useTranslation();
  const { message } = App.useApp();

  const handleCopyAll = async () => {
    const text = [
      `${t("credential_panel.login_url")}: ${loginUrl}`,
      `${t("credential_panel.account")}: ${account}`,
      `${t("credential_panel.password")}: ${password}`,
    ].join("\n");
    await navigator.clipboard.writeText(text);
    message.success(t("credential_panel.copied"));
  };

  return (
    <div data-testid="one-time-credential-panel">
      <Alert
        type="warning"
        showIcon
        message={t("credential_panel.title")}
        description={t("credential_panel.once_warning")}
        style={{ marginBottom: 16 }}
      />
      <Space direction="vertical" size="small" style={{ width: "100%" }}>
        <div>
          <Text type="secondary">{t("credential_panel.login_url")}</Text>
          <br />
          <Text copyable data-testid="otc-login-url">
            {loginUrl}
          </Text>
        </div>
        <div>
          <Text type="secondary">{t("credential_panel.account")}</Text>
          <br />
          <Text copyable data-testid="otc-account">
            {account}
          </Text>
        </div>
        <div>
          <Text type="secondary">{t("credential_panel.password")}</Text>
          <br />
          <Text code copyable data-testid="otc-password">
            {password}
          </Text>
        </div>
      </Space>
      <Button style={{ marginTop: 16 }} onClick={handleCopyAll} data-testid="otc-copy-all">
        {t("credential_panel.copy_all")}
      </Button>
    </div>
  );
}
