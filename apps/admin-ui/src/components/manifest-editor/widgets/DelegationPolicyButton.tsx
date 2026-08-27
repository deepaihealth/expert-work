/**
 * 委派增强层 3 — 配置页「生成委派策略」按钮 + 草稿预览 Modal。
 *
 * 点击调 ``POST /v1/agents/{name}/{version}/delegation-policy:generate``
 * (辅助 LLM 读该 Agent 已保存的 manifest 起草领域化委派策略),结果只读
 * 预览;「插入到提示词末尾」把草稿追加进 prompt 编辑器内容(``onInsert``
 * 由 FormView 接到 ``setSystemPrompt``),用户可继续编辑,保存走既有流程
 * — 本组件不落库。失败态用 ``App.useApp()`` 的 message 展示后端 detail
 * (项目约定:静态 message 在测试里不渲染)。
 *
 * 注意后端读的是**已保存**的 manifest(草稿按钮只在编辑页出现,创建流没
 * 有 agentRef 不渲染);编辑器里未保存的 prompt 修改不影响生成素材。
 */
import { useState } from "react";
import { App, Button, Modal, Typography } from "antd";
import { Sparkles } from "lucide-react";
import { useTranslation } from "react-i18next";

import { generateDelegationPolicy } from "../../../api/agents";
import { errMessage } from "../../../api/client";

const { Text } = Typography;

export interface AgentRef {
  name: string;
  version: string;
}

interface DelegationPolicyButtonProps {
  agentRef: AgentRef;
  /** 把草稿文本追加进 prompt 编辑器内容(追加逻辑归 FormView)。 */
  onInsert: (draft: string) => void;
}

export function DelegationPolicyButton({
  agentRef,
  onInsert,
}: DelegationPolicyButtonProps) {
  const { t } = useTranslation();
  const { message } = App.useApp();
  const [loading, setLoading] = useState(false);
  const [draft, setDraft] = useState<string | null>(null);

  const handleGenerate = async () => {
    setLoading(true);
    try {
      const result = await generateDelegationPolicy(
        agentRef.name,
        agentRef.version,
      );
      setDraft(result.draft);
    } catch (err: unknown) {
      message.error(
        `${t("agent_form.delegation_policy_failed")}: ${errMessage(err)}`,
      );
    } finally {
      setLoading(false);
    }
  };

  const handleInsert = () => {
    if (draft !== null) onInsert(draft);
    setDraft(null);
  };

  return (
    <>
      <Button
        size="small"
        icon={<Sparkles size={13} strokeWidth={1.75} />}
        loading={loading}
        data-testid="af-delegation-policy-generate"
        onClick={handleGenerate}
      >
        {t("agent_form.delegation_policy_generate")}
      </Button>
      <Modal
        open={draft !== null}
        title={t("agent_form.delegation_policy_title")}
        onCancel={() => setDraft(null)}
        footer={[
          <Button
            key="close"
            data-testid="af-delegation-policy-close"
            onClick={() => setDraft(null)}
          >
            {t("agent_form.delegation_policy_close")}
          </Button>,
          <Button
            key="insert"
            type="primary"
            data-testid="af-delegation-policy-insert"
            onClick={handleInsert}
          >
            {t("agent_form.delegation_policy_insert")}
          </Button>,
        ]}
      >
        {/* testid 放内层:antd 会把 Modal 的 data-testid 转发到
            .ant-modal-root,Playwright 视之为 hidden(照 PromptTemplateEditor
            先例)。草稿只读展示;编辑发生在插入后的 prompt 编辑器里。 */}
        <div data-testid="af-delegation-policy-modal">
          <Text type="secondary" style={{ display: "block", marginBottom: 8 }}>
            {t("agent_form.delegation_policy_hint")}
          </Text>
          <pre
            data-testid="af-delegation-policy-draft"
            style={{
              whiteSpace: "pre-wrap",
              wordBreak: "break-word",
              maxHeight: "50vh",
              overflowY: "auto",
              margin: 0,
              padding: 12,
              border: "1px solid var(--ew-border, rgba(255,255,255,0.1))",
              borderRadius: 6,
              fontFamily: "inherit",
            }}
          >
            {draft ?? ""}
          </pre>
        </div>
      </Modal>
    </>
  );
}
