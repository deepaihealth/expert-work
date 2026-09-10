/**
 * P-1 —— 对话详情页里「已被取代」的一轮:默认折叠的一层壳,摘要行标出它被哪
 * 一轮取代(取代它的那一轮也在这一页时给一个跳转链接),墓碑轮再加一枚「内容
 * 已清理」标签。里面照常渲染 ``TurnBlock``,壳不改任何轮内渲染。
 *
 * 只读展示,不提供「重新生成 / 编辑重发」入口 —— 这两个动作是对外接口的能力
 * (``docs-site/guide/chat.md`` 2.10),控制台不重跑别人的会话
 * (设计:docs/superpowers/specs/2026-09-09-regenerate-edit-resend-design.md §5)。
 */
import { Tag } from "antd";
import type { JSX, ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

/** 摘要行里 run id 只露前 8 位 —— 与轨迹账本 / 轮脚的短 id 写法一致。 */
const SHORT_RUN_ID_LEN = 8;

export interface SupersededFoldProps {
  /** 取代这一轮的新 run_id(非空 —— 调用方判过 ``supersededBy !== null``)。 */
  supersededBy: string;
  /** 这一轮的正文已清理。 */
  tombstone: boolean;
  /** 跳到取代它那一轮的路由;``null`` = 那一轮不在这一页(或本页不给深链),
   *  只显示 run id,不渲染链接。 */
  newRunHref: string | null;
  children: ReactNode;
}

export function SupersededFold({
  supersededBy,
  tombstone,
  newRunHref,
  children,
}: SupersededFoldProps): JSX.Element {
  const { t } = useTranslation();
  const label = t("conversations_detail.superseded_link", {
    runId: supersededBy.slice(0, SHORT_RUN_ID_LEN),
  });
  return (
    <details data-testid="superseded-fold" style={{ opacity: 0.75 }}>
      <summary style={{ cursor: "pointer", display: "flex", alignItems: "center", gap: 8 }}>
        <Tag color="default">{t("conversations_detail.superseded_tag")}</Tag>
        {newRunHref !== null ? (
          <Link data-testid="superseded-fold-link" to={newRunHref}>
            {label}
          </Link>
        ) : (
          <span data-testid="superseded-fold-link">{label}</span>
        )}
        {tombstone && (
          <Tag data-testid="tombstone-label">{t("conversations_detail.tombstone_label")}</Tag>
        )}
      </summary>
      <div style={{ marginTop: 8 }}>{children}</div>
    </details>
  );
}
